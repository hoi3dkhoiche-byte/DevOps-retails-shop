#!/usr/bin/env python3
"""Exercise the exact five image archives on disposable Compose and Kind stacks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from images import archive_config_id, inspect_image, sha256_file

APP = Path(__file__).resolve().parents[2]
SERVICES = ("ui", "catalog", "cart", "checkout", "orders")
RELEASE = {"ui": "ui", "catalog": "catalog", "cart": "carts", "checkout": "checkout", "orders": "orders"}
PORTS = {"ui": 8888, "catalog": 8081, "cart": 8082, "checkout": 8085, "orders": 8083}
HEALTH = {"ui": "/actuator/health", "catalog": "/health", "cart": "/actuator/health",
          "checkout": "/health", "orders": "/actuator/health"}
KIND_FIXTURES = {
    "mysql": {"repository": "docker.io/library/mysql", "tag": "8.4.11"},
    "rabbitmq": {"repository": "docker.io/library/rabbitmq", "tag": "4.2.0-management"},
    "redis": {"repository": "docker.io/library/redis", "tag": "7.4.2-alpine"},
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def json_file(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def resolve_config_digest(fetch_blob, image_ref):
    match = re.search(r"sha256:[0-9a-f]{64}$", image_ref)
    require(match is not None, "Unsupported runtime imageID: " + image_ref)
    digest, visited = match.group(), set()
    for _ in range(5):
        require(digest not in visited, "Cyclic image descriptor")
        visited.add(digest)
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "Invalid content digest")
        blob = fetch_blob(digest)
        require("sha256:" + hashlib.sha256(blob).hexdigest() == digest, "Runtime content checksum mismatch")
        data = json.loads(blob)
        if "manifests" in data:
            matches = [item for item in data["manifests"] if item.get("platform", {}).get("os") == "linux"
                       and item.get("platform", {}).get("architecture") == "amd64"]
            require(len(matches) == 1, "Expected one linux/amd64 manifest")
            digest = matches[0]["digest"]
        elif isinstance(data.get("config"), dict) and "digest" in data["config"]:
            digest = data["config"]["digest"]
        else:
            require(data.get("os") == "linux" and data.get("architecture") == "amd64"
                    and isinstance(data.get("rootfs"), dict), "Expected image config")
            return digest
    raise RuntimeError("Image descriptor nesting limit exceeded")


def junit_results(files, search):
    require(bool(files), "Cypress did not produce JUnit XML")
    passed, skipped, names = 0, 0, []
    for path in files:
        root = ET.parse(path).getroot()
        for suite in root.iter("testsuite"):
            require(not int(suite.get("failures", 0)) and not int(suite.get("errors", 0)), "Cypress suite failed")
        for case in root.iter("testcase"):
            require(case.find("failure") is None and case.find("error") is None, "Failed Cypress testcase")
            if case.find("skipped") is None:
                passed += 1
                names.append(case.get("name", ""))
            else:
                skipped += 1
    require(passed > 0, "Cypress executed zero tests or skipped all tests")
    if search:
        require(any("return results for a valid keyword" in name for name in names), "Real search test did not execute")
    return {"passed": passed, "executed": passed, "skipped": skipped, "passed_test_names": names}


class E2E:
    def __init__(self, directory, mode):
        self.directory, self.mode = directory.resolve(), mode
        self.report = APP / "dist/ci/reports/e2e" / mode
        self.report.mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ)
        run_id, attempt = self.env.get("GITHUB_RUN_ID", ""), self.env.get("GITHUB_RUN_ATTEMPT", "")
        require(run_id.isdigit() and attempt.isdigit(), "Run identity must be numeric")
        self.name, self.namespace = f"retail-ci-{run_id}-{attempt}", "retail-ci"
        self.kubeconfig = APP / "dist/ci/runtime" / (self.name + ".yaml")
        self.context, self.forwards, self.started, self.manifest = "kind-" + self.name, {}, False, None
        self.evidence = {"mode": mode, "backend_profile": "mysql-redis-opensearch", "identity_checks": []}

    def run(self, args, timeout=180, cwd=APP, binary=False):
        args = [str(arg) for arg in args]
        print("+ " + " ".join(args), flush=True)
        process = subprocess.Popen(args, cwd=cwd, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=not binary, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
            except ProcessLookupError:
                pass
            raise
        if not binary:
            with (self.report / "commands.log").open("a", encoding="utf-8") as log:
                log.write("+ " + " ".join(args) + "\n" + stdout + stderr)
        # Cypress prints assertion failures to stdout. Preserve them in the job
        # error as well as the complete commands.log artifact.
        failure_output = stderr.decode(errors="replace") if binary else (stdout + stderr)[-16000:]
        require(process.returncode == 0, "Command failed: " + " ".join(args) + "\n" + failure_output)
        return stdout

    def diagnostic(self, name, args):
        try:
            output = self.run(args, timeout=45)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            output = str(error)
        (self.report / name).write_text(output, encoding="utf-8")

    def compose(self, *args):
        return ["docker", "compose", "--project-name", self.name, "-f", "src/app/docker-compose.yml",
                "-f", "src/app/compose.override.yaml", "-f", "src/app/compose.ci.yaml", *args]

    def kubectl(self, *args):
        return ["kubectl", "--kubeconfig", self.kubeconfig, "--context", self.context,
                "--namespace", self.namespace, *args]

    def helm(self, *args):
        return ["helm", *args, "--kubeconfig", self.kubeconfig, "--kube-context", self.context,
                "--namespace", self.namespace]

    def http(self, service, path, timeout=10, method="GET", data=None):
        request = urllib.request.Request(f"http://127.0.0.1:{PORTS[service]}{path}",
                                         data=json.dumps(data).encode() if data is not None else None,
                                         headers={"Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            require(response.status in ((200,) if method == "GET" else (200, 201, 202, 204)), "HTTP request failed")
            return response.read()

    def wait_http(self, service, path, seconds=300, predicate=None):
        deadline, last = time.monotonic() + seconds, "not started"
        while time.monotonic() < deadline:
            try:
                data = self.http(service, path, timeout=5)
                if predicate is None or predicate(data):
                    return data
                last = "Readiness response did not match predicate"
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                last = str(error)
            time.sleep(2)
        raise RuntimeError(f"Readiness timeout: {service}{path}: {last}")

    def verify_compose(self):
        observed = {}
        for service in SERVICES:
            ids = self.run(self.compose("ps", "--quiet", service)).split()
            require(len(ids) == 1, "Expected one running container: " + service)
            actual = json.loads(self.run(["docker", "inspect", ids[0]]))[0]
            expected = self.manifest["images"][service]
            require(actual["State"]["Running"], service + " is not running")
            require(actual["Config"]["Image"] == expected["local_tag"], "Unexpected container tag: " + service)
            # Inspect the immutable identifier of the running container, not its
            # mutable tag. Docker 29 may expose a manifest ID in this field.
            config_id = inspect_image(actual["Image"], expected["image_id"])
            observed[service] = {"container_id": ids[0], "image_id": config_id,
                                 "docker_image_id": actual["Image"]}
        self.evidence["identity_checks"].append(observed)

    def start_compose(self):
        self.env.update(DB_PASSWORD="ci-only-password", SEARCH_ENABLED="true" if self.mode == "search" else "false")
        for service in SERVICES:
            self.env["CI_" + service.upper() + "_IMAGE"] = self.manifest["images"][service]["local_tag"]
        config = json.loads(self.run(self.compose("config", "--format", "json")))
        for service in SERVICES:
            value = config["services"][service]
            require(not value.get("build") and value.get("pull_policy") == "never", "App image may build/pull")
            require(value["image"] == self.manifest["images"][service]["local_tag"], "Wrong resolved image")
        json_file(self.report / "compose.resolved.json", config)
        self.started = True
        self.run(self.compose("up", "--detach", "--no-build", "--wait", "--wait-timeout", "360"), timeout=480)
        self.verify_compose()

    def chart_values(self, service):
        image = self.manifest["images"][service]
        values = {"fullnameOverride": RELEASE[service],
                  "image": {"repository": image["repository"], "tag": image["tag"], "digest": "", "pullPolicy": "Never"},
                  "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}}}
        if service in ("catalog", "cart", "orders"):
            values["app"] = {"persistence": {"provider": "mysql"}}
            prefix = "RETAIL_" + service.upper() + "_PERSISTENCE_"
            settings = {"ENDPOINT": "mysql:3306", "PASSWORD": "ci-only-password"}
            if service == "catalog":
                settings.update(DB_NAME="catalog_ci", USER="catalog_user")
                values["app"]["persistence"].update(endpoint="mysql:3306", database="catalog_ci")
            else:
                settings.update(USERNAME=service + "_user", SSL_MODE="REQUIRED")
                settings["DATABASE" if service == "cart" else "NAME"] = service + "_ci"
            values["extraEnv"] = [{"name": prefix + key, "value": value} for key, value in settings.items()]
            if service == "orders":
                values["app"]["messaging"] = {"provider": "rabbitmq", "rabbitmq": {
                    "secret": {"name": "orders-rabbitmq", "username": "rabbitmq", "password": "ci-only-password"}}}
                values["rabbitmq"] = {"create": True, "image": dict(KIND_FIXTURES["rabbitmq"], pullPolicy="Never")}
        elif service == "checkout":
            values.update({"app": {"persistence": {"provider": "redis"}, "endpoints": {"orders": "http://orders:80"}},
                           "redis": {"create": True, "image": dict(KIND_FIXTURES["redis"], pullPolicy="Never")},
                           "extraEnv": [{"name": "RETAIL_CHECKOUT_PERSISTENCE_REDIS_KEY_PREFIX", "value": "retail:ci:"}]})
        else:
            values["app"] = {"endpoints": {"catalog": "http://catalog:80", "carts": "http://carts:80",
                                          "checkout": "http://checkout:80", "orders": "http://orders:80"},
                             "chat": {"enabled": False}, "search": {"enabled": False}}
        return values

    def prepare_kind_fixtures(self):
        # Pull on the runner before creating pods. A registry limit must fail at
        # this bounded preparation step, not leave a pod in ImagePullBackOff.
        directory = self.kubeconfig.parent / (self.name + "-fixtures")
        directory.mkdir(parents=True, exist_ok=False)
        self.fixture_directory = directory
        prepared = {}
        for name, fixture in KIND_FIXTURES.items():
            reference = fixture["repository"] + ":" + fixture["tag"]
            for attempt in range(3):
                try:
                    self.run(["docker", "pull", "--platform", "linux/amd64", reference], timeout=180)
                    break
                except (RuntimeError, subprocess.TimeoutExpired) as error:
                    if attempt == 2:
                        raise RuntimeError(f"Fixture pull failed after 3 attempts: {reference}\n{error}") from error
                    delay = 5 * (2 ** attempt)
                    print(f"Retrying fixture {reference} in {delay}s ({attempt + 1}/3)", flush=True)
                    time.sleep(delay)
            # Docker 29 may retain a multi-platform index after a platform pull.
            # Export only amd64 so Kind never imports absent platform children.
            archive = directory / (name + ".tar")
            self.run(["docker", "image", "save", "--platform", "linux/amd64",
                      "--output", archive, reference], timeout=300)
            prepared[name] = {"reference": reference, "platform": "linux/amd64", "archive": archive.name,
                              "config_digest": archive_config_id(archive, reference),
                              "archive_sha256": sha256_file(archive)}
        self.evidence["kind_fixtures"] = prepared
        json_file(self.report / "kind-fixtures.json", prepared)

    def start_kind(self):
        image = self.env.get("KIND_NODE_IMAGE", "")
        require(re.fullmatch(r"kindest/node:v1\.35\.\d+@sha256:[0-9a-f]{64}", image), "Pin a verified Kind 1.35 image digest")
        self.kubeconfig.parent.mkdir(parents=True, exist_ok=True)
        require(not self.kubeconfig.exists(), "Refusing existing kubeconfig")
        self.prepare_kind_fixtures()
        self.started = True
        self.run(["kind", "create", "cluster", "--name", self.name, "--kubeconfig", self.kubeconfig,
                  "--image", image, "--wait", "180s"], timeout=360)
        for fixture in self.evidence["kind_fixtures"].values():
            archive = self.fixture_directory / fixture["archive"]
            require(sha256_file(archive) == fixture["archive_sha256"], "Fixture archive checksum mismatch")
            require(archive_config_id(archive, fixture["reference"]) == fixture["config_digest"],
                    "Fixture archive config mismatch")
            self.run(["kind", "load", "image-archive", "--name", self.name, archive], timeout=300)
        self.run(self.kubectl("create", "namespace", self.namespace))
        self.run(self.kubectl("apply", "-f", "scripts/ci/fixtures/mysql.json"))
        self.run(self.kubectl("rollout", "status", "statefulset/mysql", "--timeout=600s"), timeout=630)
        for service in SERVICES:
            self.run(["kind", "load", "image-archive", "--name", self.name,
                      self.directory / self.manifest["images"][service]["archive"]], timeout=300)
        for service in ("catalog", "cart", "orders", "checkout", "ui"):
            values = self.report / (service + ".values.json")
            json_file(values, self.chart_values(service))
            chart = f"src/{service}/chart"
            self.run(["helm", "lint", chart, "-f", values])
            rendered = self.run(["helm", "template", RELEASE[service], chart, "--namespace", self.namespace, "-f", values])
            (self.report / (service + ".rendered.yaml")).write_text(rendered, encoding="utf-8")
            install = self.helm("upgrade", "--install", RELEASE[service], chart, "-f", values, "--timeout", "600s", "--wait")
            self.run(install, timeout=660)
            if service == "orders":
                self.run(self.kubectl("rollout", "status", "statefulset/orders-rabbitmq", "--timeout=600s"), timeout=630)
            # Check the Deployment directly before reading runtime image IDs.
            self.run(self.kubectl("rollout", "status", "deployment/" + RELEASE[service],
                                  "--timeout=600s"), timeout=630)
        self.verify_kind()
        for service in SERVICES:
            self.start_forward(service)

    def start_forward(self, service):
        if service in self.forwards:
            self.stop_forward(self.forwards.pop(service))
        handle = (self.report / (service + ".port-forward.log")).open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(self.kubectl("port-forward", "--address", "127.0.0.1",
                                       "service/" + RELEASE[service], str(PORTS[service]) + ":80"),
                                       cwd=APP, env=self.env, stdout=handle, stderr=subprocess.STDOUT)
        except BaseException:
            handle.close()
            raise
        self.forwards[service] = process, handle

    @staticmethod
    def stop_forward(forward):
        process, handle = forward
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        handle.close()

    def verify_kind(self):
        pods = json.loads(self.run(self.kubectl("get", "pods", "-o", "json")))["items"]
        nodes = set(self.run(["kind", "get", "nodes", "--name", self.name]).split())
        observed = {}
        for service in SERVICES:
            deployment = json.loads(self.run(self.kubectl("get", "deployment", RELEASE[service], "-o", "json")))
            selector = deployment["spec"]["selector"]["matchLabels"]
            matches = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")
                       and all(pod["metadata"].get("labels", {}).get(key) == value for key, value in selector.items())]
            require(len(matches) == 1, "Expected one application pod: " + service)
            pod, name = matches[0], "carts" if service == "cart" else service
            spec = next(item for item in pod["spec"]["containers"] if item["name"] == name)
            status = next(item for item in pod["status"]["containerStatuses"] if item["name"] == name)
            expected = self.manifest["images"][service]
            require(status.get("ready") and "running" in status.get("state", {}), service + " is not ready")
            require(spec["image"] == expected["local_tag"] and spec["imagePullPolicy"] == "Never", "Unexpected pod image/pull policy")
            node = pod["spec"]["nodeName"]
            require(node in nodes, "Pod is outside this disposable Kind cluster")
            actual = resolve_config_digest(
                lambda digest: self.run(["docker", "exec", node, "ctr", "--namespace", "k8s.io", "content", "get", digest], binary=True),
                status["imageID"])
            require(actual == expected["image_id"], "Pod runtime config ID differs from tested archive: " + service)
            observed[service] = {"pod": pod["metadata"]["name"], "node": node,
                                 "runtime_image_id": status["imageID"], "config_digest": actual}
        self.evidence["identity_checks"].append(observed)

    def smoke(self):
        for service in SERVICES:
            self.wait_http(service, HEALTH[service])
        for service, provider in (("catalog", "mysql"), ("cart", "mysql"), ("orders", "mysql")):
            topology = json.loads(self.http(service, "/topology"))
            require(topology.get("persistenceProvider") == provider, "Wrong backend provider: " + service)
            if service == "orders":
                require(topology.get("messagingProvider") == "rabbitmq", "Orders did not use RabbitMQ")
        products = json.loads(self.http("catalog", "/catalog/products"))
        require(isinstance(products, list) and bool(products), "No seeded catalog products")
        require(isinstance(json.loads(self.http("cart", "/carts/ci-smoke")).get("items"), list), "Cart backend failed")
        require(isinstance(json.loads(self.http("orders", "/orders")), list), "Orders backend failed")
        if self.mode == "search":
            self.wait_http("catalog", "/catalog/search?keyword=shoe",
                           predicate=lambda body: isinstance(json.loads(body), list) and bool(json.loads(body)))

    def cypress(self):
        require(not list(self.report.glob("junit-*.xml")), "Stale Cypress reports")
        directory = APP / "src/e2e"
        self.run(["yarn", "install", "--immutable"], cwd=directory, timeout=300)
        self.env.update(CYPRESS_BASE_URL="http://127.0.0.1:8888",
                        CYPRESS_SEARCH_ENABLED="true" if self.mode == "search" else "false")
        self.run(["yarn", "exec", "cypress", "run", "--browser", "electron", "--headless",
                  "--reporter", "junit", "--reporter-options", f"mochaFile={self.report}/junit-[hash].xml,toConsole=true",
                  "--config", f"video=false,screenshotsFolder={self.report}/screenshots"], cwd=directory, timeout=1200)
        self.evidence["cypress"] = junit_results(list(self.report.glob("junit-*.xml")), self.mode == "search")

    def persistence(self):
        before = json.loads(self.http("orders", "/orders"))
        require(bool(before), "Cypress did not create an order in the real Orders backend")
        query = ["env", "MYSQL_PWD=ci-only-password", "mysql", "-h127.0.0.1", "-uorders_user", "orders_ci", "-Nse", "select count(*) from orders;"]
        if self.mode == "kind":
            count = self.run(self.kubectl("exec", "mysql-0", "--", *query))
            self.run(self.kubectl("rollout", "restart", "deployment/orders"))
            self.run(self.kubectl("rollout", "status", "deployment/orders", "--timeout=300s"), timeout=330)
            self.start_forward("orders")
        else:
            count = self.run(self.compose("exec", "-T", "mysql", *query))
            self.run(self.compose("restart", "orders"), timeout=90)
        require(int(count.strip()) >= len(before), "SQL did not contain submitted orders")
        self.wait_http("orders", HEALTH["orders"])
        after = json.loads(self.http("orders", "/orders"))
        require({item["id"] for item in before} <= {item["id"] for item in after}, "Orders disappeared after restart")
        self.evidence["orders_persistence"] = {"orders_before_restart": len(before), "database_rows": int(count.strip()),
                                              "orders_after_restart": len(after)}
        self.verify_kind() if self.mode == "kind" else self.verify_compose()
        customer = self.name
        item = {"itemId": "ci-persistence-item", "quantity": 3, "unitPrice": 123}
        self.http("cart", f"/carts/{customer}/items", method="POST", data=item)
        before_cart = json.loads(self.http("cart", f"/carts/{customer}/items"))
        require(before_cart == [item], "Cart did not save the submitted quantity and price")
        require(re.fullmatch(r"[a-z0-9-]+", customer), "Unsafe fixture customer identifier")
        query = ["env", "MYSQL_PWD=ci-only-password", "mysql", "-h127.0.0.1", "-ucart_user", "cart_ci", "-Nse",
                 f"SELECT quantity FROM cart_items WHERE customer_id='{customer}' AND item_id='ci-persistence-item';"]
        if self.mode == "kind":
            quantity = self.run(self.kubectl("exec", "mysql-0", "--", *query))
            self.run(self.kubectl("rollout", "restart", "deployment/carts"))
            self.run(self.kubectl("rollout", "status", "deployment/carts", "--timeout=300s"), timeout=330)
            self.start_forward("cart")
        else:
            quantity = self.run(self.compose("exec", "-T", "mysql", *query))
            self.run(self.compose("restart", "cart"), timeout=90)
        require(int(quantity.strip()) == item["quantity"], "Cart SQL quantity differs from the API")
        self.wait_http("cart", HEALTH["cart"])
        after_cart = json.loads(self.http("cart", f"/carts/{customer}/items"))
        require(after_cart == before_cart, "Cart contents changed after restart")
        self.evidence["cart_persistence"] = {"items_before_restart": len(before_cart), "database_quantity": int(quantity.strip()),
                                            "items_after_restart": len(after_cart), "quantity_after_restart": after_cart[0]["quantity"],
                                            "unit_price_after_restart": after_cart[0]["unitPrice"]}
        self.verify_kind() if self.mode == "kind" else self.verify_compose()

    def diagnostics(self):
        if not self.started:
            return
        if self.mode == "kind":
            self.diagnostic("pods.json", self.kubectl("get", "pods", "-o", "json"))
            self.diagnostic("events.txt", self.kubectl("get", "events", "--sort-by=.lastTimestamp"))
            self.diagnostic("describe.txt", self.kubectl("describe", "pods"))
            self.diagnostic("mysql.log", self.kubectl("logs", "statefulset/mysql", "--tail=300"))
            self.diagnostic("mysql.previous.log", self.kubectl("logs", "statefulset/mysql", "--previous", "--tail=300"))
            self.diagnostic("rabbitmq.log", self.kubectl("logs", "statefulset/orders-rabbitmq", "--tail=300"))
            self.diagnostic("rabbitmq.previous.log", self.kubectl("logs", "statefulset/orders-rabbitmq", "--previous", "--tail=300"))
            self.diagnostic("redis.log", self.kubectl("logs", "deployment/checkout-redis", "--tail=300"))
            for service in SERVICES:
                self.diagnostic(service + ".log", self.kubectl("logs", "deployment/" + RELEASE[service], "--all-containers=true", "--tail=300"))
        else:
            self.diagnostic("compose.ps.json", self.compose("ps", "--all", "--format", "json"))
            self.diagnostic("compose.log", self.compose("logs", "--no-color", "--tail", "300"))

    def cleanup(self):
        for forward in self.forwards.values():
            self.stop_forward(forward)
        try:
            if self.started:
                if self.mode == "kind":
                    self.run(["kind", "delete", "cluster", "--name", self.name], timeout=180)
                else:
                    self.run(self.compose("down", "--volumes", "--remove-orphans", "--timeout", "30"), timeout=120)
        finally:
            if self.started:
                self.kubeconfig.unlink(missing_ok=True)
            directory = getattr(self, "fixture_directory", None)
            if directory is not None:
                for name in KIND_FIXTURES:
                    (directory / (name + ".tar")).unlink(missing_ok=True)
                directory.rmdir()

    def execute(self):
        self.run([sys.executable, APP / "scripts/ci/images.py", "verify", "--directory", self.directory, "--load"], timeout=600)
        self.manifest = json.loads((self.directory / "metadata.json").read_text(encoding="utf-8"))
        self.evidence.update(source_commit=self.manifest["source_commit"], run=self.manifest["run"])
        try:
            self.start_kind() if self.mode == "kind" else self.start_compose()
            self.smoke()
            self.cypress()
            self.persistence()
        except BaseException as error:
            self.evidence.update(status="failed", error=str(error))
            json_file(self.report / "evidence.json", self.evidence)
            raise
        finally:
            try:
                self.diagnostics()
            finally:
                self.cleanup()
        self.evidence["status"] = "passed"
        json_file(self.report / "evidence.json", self.evidence)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--mode", choices=("compose", "search", "kind"), required=True)
    args = parser.parse_args()

    def interrupted(signum, _frame):
        raise RuntimeError("Interrupted by signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    E2E(args.images, args.mode).execute()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        print("E2E failed: " + str(error), file=sys.stderr)
        sys.exit(1)

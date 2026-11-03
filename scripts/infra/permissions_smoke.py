#!/usr/bin/env python3
"""Exercise bootstrap permissions on disposable loopback RabbitMQ/OpenSearch fixtures.

Requires pika==1.3.2. Start security-enabled OpenSearch and RabbitMQ management
fixtures, set PERMISSIONS_FIXTURE_ADMIN_PASSWORD, then run this script. It creates
the retail-dev/prod test users, vhosts and products indices in those fixtures.
Never point this probe at a real backend; all URLs must use a loopback address.
"""
import argparse
import base64
import copy
import json
import os
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import bootstrap_backends as bootstrap


class FixtureSecrets:
    def __init__(self):
        self.values = {}

    def describe_secret(self, SecretId):
        return {"ARN": SecretId, "VersionIdsToStages": {"fixture": ["AWSCURRENT"]} if SecretId in self.values else {}}

    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps(self.values[SecretId])}

    def put_secret_value(self, SecretId, SecretString):
        self.values[SecretId] = json.loads(SecretString)


def loopback(value):
    parsed = urlparse(value)
    if (parsed.scheme not in {"http", "https", "amqp"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password):
        raise ValueError("Permission probes require disposable loopback URLs without embedded credentials")
    return value.rstrip("/")


def api(base, method, path, credentials, payload=None, raw=None, timeout=15):
    token = base64.b64encode((credentials["username"] + ":" + credentials["password"]).encode()).decode()
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    request = Request(base + path, data=data, method=method,
                      headers={"Authorization": "Basic " + token,
                               "Content-Type": "application/x-ndjson" if raw is not None else "application/json"})
    # Demo certificates are accepted only for a URL already constrained to loopback.
    context = ssl._create_unverified_context() if base.startswith("https://") else None
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            return response.status, json.loads(response.read() or b"null")
    except HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def wait_ready(base, path, admin):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            status, _ = api(base, "GET", path, admin, timeout=5)
            require(status not in (401, 403), "Fixture administrator authentication failed")
            if status == 200:
                return
        except (URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(1)
    raise TimeoutError("Fixture management API did not become ready: " + base)


def putter(base, legacy=False):
    def put(_endpoint, path, credentials, payload):
        payload = copy.deepcopy(payload)
        if legacy and "/permissions/" in path:
            payload.update(write=r"^orders\.(events|dlx)$", read=r"^orders\.events\.(v2|dlq)$")
        if legacy and "/roles/" in path:
            payload["cluster_permissions"] = ["cluster:monitor/main", "cluster:monitor/health"]
        status, _ = api(base, "PUT", path, credentials, payload)
        require(status in (200, 201, 204), "Fixture admin operation failed: " + path + " HTTP " + str(status))
    return put


def rabbit_connection(endpoint, credentials, vhost):
    import pika
    parsed = urlparse(endpoint)
    return pika.BlockingConnection(pika.ConnectionParameters(
        host=parsed.hostname, port=parsed.port or 5672, virtual_host=vhost,
        credentials=pika.PlainCredentials(credentials["username"], credentials["password"]),
        socket_timeout=10, blocked_connection_timeout=10))


def rabbit_topology(channel):
    for exchange in ("orders.events", "orders.dlx"):
        channel.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)
    channel.queue_declare(queue="orders.events.v2", durable=True,
                         arguments={"x-queue-type": "quorum", "x-dead-letter-exchange": "orders.dlx",
                                    "x-dead-letter-routing-key": "orders.failed"})
    channel.queue_declare(queue="orders.events.dlq", durable=True, arguments={"x-queue-type": "quorum"})
    channel.queue_bind(queue="orders.events.v2", exchange="orders.events", routing_key="orders.created")
    channel.queue_bind(queue="orders.events.dlq", exchange="orders.dlx", routing_key="orders.failed")


def rabbit_denied(endpoint, credentials, vhost, operation):
    import pika
    connection = None
    try:
        connection = rabbit_connection(endpoint, credentials, vhost)
        operation(connection.channel())
    except (pika.exceptions.ChannelClosedByBroker, pika.exceptions.ProbableAccessDeniedError) as error:
        require(getattr(error, "reply_code", 403) in (403, 530), "Unexpected AMQP denial code")
        return
    finally:
        if connection is not None and connection.is_open:
            connection.close()
    raise AssertionError("RabbitMQ unexpectedly allowed forbidden operation")


def get_message(channel, queue):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        method, _properties, body = channel.basic_get(queue=queue, auto_ack=False)
        if method:
            return method, body
        time.sleep(0.1)
    raise AssertionError("No fixture message arrived in " + queue)


def verify_rabbit(store, management, endpoint, admin, prove_legacy):
    synthetic = "amqps://permission-fixture.invalid:5671"
    if prove_legacy:
        bootstrap.rabbit_accounts(store, synthetic, admin, put=putter(management, legacy=True))
        rabbit_denied(endpoint, store.values["/retail/dev/orders/rabbitmq"], "retail-dev", rabbit_topology)
        print("PASS RabbitMQ legacy permission rejects topology with ACCESS_REFUSED")
    bootstrap.rabbit_accounts(store, synthetic, admin, put=putter(management))
    for environment in bootstrap.ENVIRONMENTS:
        user = store.values[f"/retail/{environment}/orders/rabbitmq"]
        connection = rabbit_connection(endpoint, user, "retail-" + environment)
        try:
            channel = connection.channel()
            rabbit_topology(channel)
            channel.confirm_delivery()
            channel.basic_publish(exchange="orders.events", routing_key="orders.created", body=b"fixture-order", mandatory=True)
            method, body = get_message(channel, "orders.events.v2")
            require(body == b"fixture-order", "Orders queue payload mismatch")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            dead, body = get_message(channel, "orders.events.dlq")
            require(body == b"fixture-order", "Dead-letter payload mismatch")
            channel.basic_ack(delivery_tag=dead.delivery_tag)
        finally:
            connection.close()
        other = "prod" if environment == "dev" else "dev"
        rabbit_denied(endpoint, user, "retail-" + environment,
                      lambda channel: channel.queue_declare(queue="unrelated.queue", durable=True))
        rabbit_denied(endpoint, user, "retail-" + other, lambda channel: None)
        print("PASS RabbitMQ " + environment + ": AMQP topology, publish/consume, DLQ, unrelated-resource and cross-vhost denial")


def bulk(search, user, index):
    payload = (json.dumps({"index": {"_index": index, "_id": "permission-fixture"}}) + "\n"
               + json.dumps({"name": "fixture-product"}) + "\n").encode()
    return api(search, "POST", "/_bulk?refresh=true", user, raw=payload)


def isolate_search_roles(search, admin):
    # The stock demo maps every authenticated user to own_index, which grants
    # cluster_composite_ops and would mask a missing grant in our Catalog role.
    status, mappings = api(search, "GET", "/_plugins/_security/api/rolesmapping/own_index", admin)
    require(status == 200, "Cannot inspect the disposable OpenSearch demo role mapping")
    mapping = mappings["own_index"]
    if "*" in mapping.get("users", []):
        payload = {key: mapping.get(key, []) for key in ("users", "backend_roles", "hosts", "and_backend_roles")}
        payload["users"] = [user for user in payload["users"] if user != "*"]
        status, _ = api(search, "PUT", "/_plugins/_security/api/rolesmapping/own_index", admin, payload)
        require(status in (200, 201), "Cannot isolate Catalog from the fixture's wildcard demo role")


def require_catalog_role(search, user, environment):
    status, auth = api(search, "GET", "/_plugins/_security/authinfo", user)
    require(status == 200 and set(auth.get("roles", [])) == {"retail-catalog-" + environment},
            "OpenSearch fixture must test only the environment Catalog role, without demo grants")


def verify_search(store, search, admin, prove_legacy):
    isolate_search_roles(search, admin)
    for environment in bootstrap.ENVIRONMENTS:
        status, _ = api(search, "DELETE", "/products-" + environment, admin)
        require(status in (200, 404), "Cannot reset disposable fixture index")
        status, _ = api(search, "PUT", "/products-" + environment, admin,
                        {"settings": {"number_of_shards": 1, "number_of_replicas": 0}})
        require(status in (200, 201), "Cannot create disposable fixture index")
    if prove_legacy:
        bootstrap.search_accounts(store, search, admin, put=putter(search, legacy=True))
        require_catalog_role(search, store.values["/retail/dev/catalog/opensearch"], "dev")
        status, _ = bulk(search, store.values["/retail/dev/catalog/opensearch"], "products-dev")
        require(status == 403, "Legacy Catalog role did not reproduce missing cluster bulk permission")
        print("PASS OpenSearch legacy permission rejects bulk with HTTP 403")
    bootstrap.search_accounts(store, search, admin, put=putter(search))
    for environment in bootstrap.ENVIRONMENTS:
        user = store.values[f"/retail/{environment}/catalog/opensearch"]
        index = "products-" + environment
        require_catalog_role(search, user, environment)
        status, _ = api(search, "GET", "/", user)
        require(status == 200, "Catalog cannot inspect OpenSearch as its configured user")
        # Catalog initialization/reindex owns creation, deletion and mappings in this one index.
        status, _ = api(search, "DELETE", "/" + index, user)
        require(status in (200, 404), "Catalog cannot delete its own index")
        status, _ = api(search, "PUT", "/" + index, user,
                        {"settings": {"number_of_shards": 1, "number_of_replicas": 0},
                         "mappings": {"properties": {"name": {"type": "text"}}}})
        require(status in (200, 201), "Catalog cannot create its own index")
        status, response = bulk(search, user, index)
        require(status == 200 and response.get("errors") is False, "Catalog bulk failed")
        status, response = api(search, "GET", "/" + index + "/_search", user)
        require(status == 200 and response["hits"]["total"]["value"] == 1, "Catalog search cannot read indexed product")
        other = "products-" + ("prod" if environment == "dev" else "dev")
        status, response = bulk(search, user, other)
        require(status == 403 or (status == 200 and response.get("errors") is True
                                and all(item["index"].get("status") == 403 for item in response["items"])),
                "Catalog role escaped its own index")
        print("PASS OpenSearch " + environment + ": create/delete, bulk/search and cross-index denial")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rabbit-management", default="http://127.0.0.1:15683")
    parser.add_argument("--rabbit-amqp", default="amqp://127.0.0.1:5683")
    parser.add_argument("--search", default="https://127.0.0.1:19283")
    parser.add_argument("--prove-legacy", action="store_true")
    args = parser.parse_args()
    management, rabbit, search = map(loopback, (args.rabbit_management, args.rabbit_amqp, args.search))
    password = os.environ["PERMISSIONS_FIXTURE_ADMIN_PASSWORD"]
    admin = {"username": "admin", "password": password}
    wait_ready(management, "/api/overview", admin)
    wait_ready(search, "/_cluster/health", admin)
    store = FixtureSecrets()
    verify_rabbit(store, management, rabbit, admin, args.prove_legacy)
    verify_search(store, search, admin, args.prove_legacy)
    print("Backend permission smoke passed; disposable fixtures only, no AWS validation claimed.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Initialize existing private backends and store credentials directly in Secrets Manager.

init-platform runs once with the operator AWS profile before Terraform.
configure runs from inside the VPC after Terraform; it never prints secret values.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import secrets
import ssl
import sys
from urllib.parse import quote, urlparse
import urllib.request

REGION = "ap-southeast-1"
ENVIRONMENTS = ("dev", "prod")
SERVICES = ("catalog", "cart", "orders")
PLATFORM = {
    "mq_admin": ("/retail/platform/mq/admin", "retailadmin"),
    "search_admin": ("/retail/platform/search/admin", "retailadmin"),
    "redis_dev": ("/retail/platform/redis/dev", "checkout-dev"),
    "redis_prod": ("/retail/platform/redis/prod", "checkout-prod"),
}


def output_value(outputs, key):
    value = outputs[key]
    return value["value"] if isinstance(value, dict) and "value" in value else value


def read_secret(client, identifier, create_metadata=False):
    try:
        metadata = client.describe_secret(SecretId=identifier)
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") != "ResourceNotFoundException" or not create_metadata:
            raise
        metadata = client.create_secret(Name=identifier, Description="Retail lab credential; never stored in Terraform state")
    if metadata.get("DeletedDate"):
        raise ValueError("A required secret is scheduled for deletion")
    arn = metadata["ARN"]
    if not any("AWSCURRENT" in stages for stages in metadata.get("VersionIdsToStages", {}).values()):
        return None, arn
    value = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
    if not isinstance(value, dict):
        raise ValueError("Secret must contain a JSON object")
    return value, arn


def write_secret(client, arn, value):
    client.put_secret_value(SecretId=arn, SecretString=json.dumps(value, separators=(",", ":")))


def credentials(client, identifier, username, extra=None, create_metadata=False):
    value, arn = read_secret(client, identifier, create_metadata=create_metadata)
    if value is None:
        value = {"username": username, "password": secrets.token_urlsafe(36)}
    if value.get("username") != username or not isinstance(value.get("password"), str) or len(value["password"]) < 16:
        raise ValueError("Credential username/password does not match the documented service account")
    updated = dict(value, **(extra or {}))
    if value is None or updated != value:
        write_secret(client, arn, updated)
    else:
        # A newly generated credential still needs its first AWSCURRENT version.
        metadata = client.describe_secret(SecretId=arn)
        if not any("AWSCURRENT" in stages for stages in metadata.get("VersionIdsToStages", {}).values()):
            write_secret(client, arn, updated)
    return updated, arn


def init_platform(client, output):
    result = {}
    for key, (name, username) in PLATFORM.items():
        _, result[key] = credentials(client, name, username, create_metadata=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print("Platform secret ARNs written; credential values remain in Secrets Manager.")


def request_json(endpoint, path, credentials, payload):
    if not endpoint.startswith("https://"):
        raise ValueError("Backend management API must use HTTPS")
    token = base64.b64encode((credentials["username"] + ":" + credentials["password"]).encode()).decode()
    request = urllib.request.Request(endpoint.rstrip("/") + path,
        data=json.dumps(payload).encode(), method="PUT",
        headers={"Content-Type": "application/json", "Authorization": "Basic " + token})
    with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=30) as response:
        if response.status not in (200, 201, 204):
            raise ValueError("Backend administration returned a non-success status")


def mysql_accounts(client, connection, host):
    with connection.cursor() as cursor:
        for environment in ENVIRONMENTS:
            for service in SERVICES:
                database = service + "_" + environment
                username = database
                value, _ = credentials(client, f"/retail/{environment}/{service}/mysql", username,
                    extra={"host": host, "port": 3306, "database": database})
                # Identifiers are generated exclusively from these fixed service/environment enums.
                assert re.fullmatch(r"[a-z_]+", database)
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
                cursor.execute("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s REQUIRE SSL", (username, value["password"]))
                cursor.execute("ALTER USER %s@'%%' IDENTIFIED BY %s REQUIRE SSL", (username, value["password"]))
                cursor.execute(f"GRANT ALL PRIVILEGES ON `{database}`.* TO %s@'%%'", (username,))
    connection.commit()


def rabbit_accounts(client, endpoint, admin, put=request_json):
    parsed = urlparse(endpoint)
    if parsed.scheme != "amqps" or parsed.port != 5671 or not parsed.hostname:
        raise ValueError("Expected a private AMQPS broker endpoint on port 5671")
    management = "https://" + parsed.hostname
    for environment in ENVIRONMENTS:
        vhost, username = "retail-" + environment, "orders-" + environment
        value, _ = credentials(client, f"/retail/{environment}/orders/rabbitmq", username,
            extra={"addresses": endpoint, "vhost": vhost})
        put(management, "/api/vhosts/" + quote(vhost, safe=""), admin, {})
        put(management, "/api/users/" + quote(username, safe=""), admin, {"password": value["password"], "tags": ""})
        put(management, "/api/permissions/" + quote(vhost, safe="") + "/" + quote(username, safe=""), admin,
            {"configure": r"^orders\.(events(\.v2|\.dlq)?|dlx)$", "write": r"^orders\.(events|dlx)$", "read": r"^orders\.events\.(v2|dlq)$"})


def search_accounts(client, endpoint, admin, put=request_json):
    endpoint = endpoint if endpoint.startswith("https://") else "https://" + endpoint
    for environment in ENVIRONMENTS:
        username, role = "catalog-" + environment, "retail-catalog-" + environment
        value, _ = credentials(client, f"/retail/{environment}/catalog/opensearch", username, extra={"endpoint": endpoint})
        put(endpoint, "/_plugins/_security/api/roles/" + role, admin, {
            "cluster_permissions": ["cluster:monitor/main", "cluster:monitor/health"],
            "index_permissions": [{"index_patterns": ["products-" + environment], "allowed_actions": ["indices_all"]}], "tenant_permissions": []})
        put(endpoint, "/_plugins/_security/api/internalusers/" + username, admin, {"password": value["password"], "backend_roles": []})
        put(endpoint, "/_plugins/_security/api/rolesmapping/" + role, admin, {"users": [username], "backend_roles": [], "hosts": []})


def redis_urls(client, host, platform):
    for environment in ENVIRONMENTS:
        value, _ = read_secret(client, platform["redis_" + environment])
        if not value or value.get("username") != "checkout-" + environment or not value.get("password"):
            raise ValueError("Redis platform credential is missing or names another environment")
        url = "rediss://" + quote(value["username"], safe="") + ":" + quote(value["password"], safe="") + "@" + host + ":6379"
        existing, arn = read_secret(client, f"/retail/{environment}/checkout/redis")
        if existing != {"url": url}:
            write_secret(client, arn, {"url": url})


def argocd_credentials(client):
    import bcrypt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    value, arn = credentials(client, "/retail/platform/argocd/admin", "admin")
    updated = dict(value)
    encoded_password = value["password"].encode()
    if not value.get("password_hash") or not bcrypt.checkpw(encoded_password, value["password_hash"].encode()):
        updated["password_hash"] = bcrypt.hashpw(encoded_password, bcrypt.gensalt(rounds=12)).decode()
        updated["password_mtime"] = datetime.now(timezone.utc).isoformat()
    updated.setdefault("server_secret_key", base64.b64encode(secrets.token_bytes(32)).decode())
    renew = not (value.get("tls_certificate") and value.get("tls_private_key"))
    if not renew:
        certificate = x509.load_pem_x509_certificate(value["tls_certificate"].encode())
        private_key = serialization.load_pem_private_key(value["tls_private_key"].encode(), password=None)
        if private_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != certificate.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
            raise ValueError("Existing Argo CD certificate and private key do not match")
        renew = certificate.not_valid_after_utc < datetime.now(timezone.utc) + timedelta(days=30)
    if renew:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "argocd-server")])
        now = datetime.now(timezone.utc)
        certificate = x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(private_key.public_key()) \
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365)) \
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in
                ["argocd-server", "argocd-server.argocd.svc", "argocd-server.argocd.svc.cluster.local", "localhost"]]), critical=False) \
            .sign(private_key, hashes.SHA256())
        updated["tls_certificate"] = certificate.public_bytes(serialization.Encoding.PEM).decode()
        updated["tls_private_key"] = private_key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    if updated != value:
        write_secret(client, arn, updated)
    existing, redis_arn = read_secret(client, "/retail/platform/argocd/redis")
    if not existing:
        write_secret(client, redis_arn, {"password": secrets.token_urlsafe(36)})
    elif not isinstance(existing.get("password"), str) or len(existing["password"]) < 16:
        raise ValueError("Existing Argo CD Redis credential is invalid")


def configure(client, outputs, ca_file):
    import pymysql
    host = output_value(outputs, "mysql_endpoint")
    master, _ = read_secret(client, output_value(outputs, "mysql_master_secret_arn"))
    if not master or not ca_file.is_file():
        raise ValueError("RDS managed master credential and an official RDS CA bundle are required")
    connection = pymysql.connect(host=host, port=3306, user=master["username"], password=master["password"],
        ssl_ca=str(ca_file), ssl_verify_cert=True, ssl_verify_identity=True, connect_timeout=10, read_timeout=30, write_timeout=30)
    try:
        mysql_accounts(client, connection, host)
    finally:
        connection.close()
    platform = output_value(outputs, "platform_secret_arns")
    endpoints = output_value(outputs, "backend_endpoints")
    mq_admin, _ = read_secret(client, platform["mq_admin"])
    search_admin, _ = read_secret(client, platform["search_admin"])
    if not mq_admin or not search_admin:
        raise ValueError("Platform administrator credentials are unavailable")
    rabbit_accounts(client, endpoints["RabbitMQEndpoint"], mq_admin)
    search_accounts(client, endpoints["SearchEndpoint"], search_admin)
    redis_urls(client, endpoints["RedisEndpoint"], platform)
    credentials(client, "/retail/platform/grafana/admin", "admin")
    argocd_credentials(client)
    print("Configured six MySQL databases/users, RabbitMQ vhosts/users, OpenSearch roles, Redis URLs and platform credentials.")
    print("Set the optional Alertmanager notification configuration after selecting a recipient.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("init-platform", "configure"))
    parser.add_argument("--terraform-output", type=Path)
    parser.add_argument("--rds-ca", type=Path, default=Path("gitops/applications/files/rds-ca.pem"))
    parser.add_argument("--arn-output", type=Path, default=Path(".ci-tools/platform-secret-arns.json"))
    args = parser.parse_args()
    import boto3
    client = boto3.client("secretsmanager", region_name=REGION)
    if args.operation == "init-platform":
        init_platform(client, args.arn_output)
    else:
        if not args.terraform_output:
            parser.error("configure requires --terraform-output")
        configure(client, json.loads(args.terraform_output.read_text()), args.rds_ca)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # SDK/SQL exception strings may contain request details; never print them.
        print("Backend bootstrap failed (" + type(error).__name__ + "). Check prerequisite configuration and private connectivity.", file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
"""Record dev acceptance only after checking live pods and HTTP smoke endpoints."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from release import COMMIT, DIGEST, SERVICES, json_text, parse_json, release_from_values, require, validate_acceptance

NAMES = {service: "carts" if service == "cart" else service for service in SERVICES}


def run(args, timeout=60):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    require(result.returncode == 0, "Command failed: " + " ".join(args[:4]) + "; " + result.stderr.decode(errors="replace")[-800:])
    return result.stdout


def manifest(reference, digest):
    raw = run(["docker", "buildx", "imagetools", "inspect", "--raw", reference], timeout=90)
    # buildx adds one display newline to the registry's original JSON bytes.
    require(any("sha256:" + hashlib.sha256(candidate).hexdigest() == digest for candidate in (raw, raw.removesuffix(b"\n"))),
            "Registry manifest bytes do not match the requested digest")
    return parse_json(raw)


def runtime_digests(image):
    """Resolve the amd64 manifest through an OCI index and link it to CI's config ID."""
    digest = image["digest"]
    document = manifest(image["reference"], digest)
    allowed = {digest, image["image_id"]}
    if "manifests" in document:
        entries = [entry for entry in document["manifests"]
                   if entry.get("platform", {}).get("os") == "linux" and entry.get("platform", {}).get("architecture") == "amd64"]
        require(len(entries) == 1 and DIGEST.fullmatch(entries[0].get("digest", "")), "Expected one linux/amd64 image manifest")
        digest = entries[0]["digest"]
        allowed.add(digest)
        document = manifest(image["repository"] + "@" + digest, digest)
    require(document.get("config", {}).get("digest") == image["image_id"], "Registry config differs from the image tested by CI")
    return allowed


def kubectl(context, *args):
    return ["kubectl", "--context", context, "--namespace", "retail-dev", *args]


def inspect_runtime(context, release, resolved):
    evidence = {}
    for service in SERVICES:
        name = NAMES[service]
        kind = "rollout.argoproj.io" if service == "ui" else "deployment"
        workload = parse_json(run(kubectl(context, "get", kind + "/" + name, "-o", "json")))
        spec, status = workload["spec"], workload.get("status", {})
        require(int(status.get("observedGeneration", -1)) >= workload["metadata"]["generation"], "Controller has not observed the desired generation: " + service)
        if service == "ui":
            require(status.get("phase") == "Healthy" and status.get("currentPodHash")
                    and status.get("stableRS") == status.get("currentPodHash"), "UI dev Rollout is not fully promoted and healthy")
            active = parse_json(run(kubectl(context, "get", "service/ui-active", "-o", "json")))
            selector = active["spec"]["selector"]
        else:
            require(status.get("availableReplicas", 0) >= spec.get("replicas", 1) > 0, "Unavailable dev replicas: " + service)
            selector = spec["selector"]["matchLabels"]
        require(selector, "Empty workload selector")
        label = ",".join(key + "=" + value for key, value in sorted(selector.items()))
        pods = parse_json(run(kubectl(context, "get", "pods", "-l", label, "-o", "json")))["items"]
        pods = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
        require(bool(pods), "No active dev pod: " + service)
        records = []
        for pod in pods:
            container = next((item for item in pod["spec"]["containers"] if item["name"] == name), None)
            state = next((item for item in pod.get("status", {}).get("containerStatuses", []) if item["name"] == name), None)
            require(container and state and state.get("ready") is True and "running" in state.get("state", {}), "Unready runtime container: " + service)
            require(container["image"] == release["images"][service]["reference"], "Dev is running another release: " + service)
            match = re.search(r"sha256:[0-9a-f]{64}$", state.get("imageID", ""))
            require(match and match.group() in resolved[service], "Dev runtime image identity differs from CI: " + service)
            records.append({"name": pod["metadata"]["name"], "ready": True, "image_reference": container["image"],
                            "runtime_image_id": state["imageID"], "verified_config_digest": release["images"][service]["image_id"]})
        evidence[service] = records
    return evidence


@contextmanager
def forward(context, service):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    name = "ui-active" if service == "ui" else NAMES[service]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(kubectl(context, "port-forward", "--address", "127.0.0.1", "service/" + name, str(port) + ":80"),
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while True:
                require(process.poll() is None, "Dev port-forward exited: " + service)
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    require(time.monotonic() < deadline, "Dev port-forward timed out: " + service)
                    time.sleep(0.2)
            yield "http://127.0.0.1:" + str(port)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def http(url, method="GET", html=False):
    request = urllib.request.Request(url, method=method, headers={"Accept": "text/html" if html else "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        require(200 <= response.status < 300, "Dev smoke request failed")
        return response.read(4 * 1024 * 1024)


def smoke(context):
    with forward(context, "ui") as url:
        require(b"<html" in http(url + "/", html=True).lower(), "UI did not render the store HTML")
    with forward(context, "catalog") as url:
        products = parse_json(http(url + "/catalog/products"))
        require(isinstance(products, list) and bool(products), "Catalog SQL has no products")
        keyword = urllib.parse.quote(products[0]["name"], safe="")
        results = parse_json(http(url + "/catalog/search?keyword=" + keyword))
        require(isinstance(results, list) and bool(results), "Catalog search has no indexed SQL products")
    with forward(context, "cart") as url:
        customer = "gitops-acceptance-" + str(time.time_ns())
        try:
            require(isinstance(parse_json(http(url + "/carts/" + customer)).get("items"), list), "Cart backend smoke failed")
        finally:
            http(url + "/carts/" + customer, method="DELETE")
    with forward(context, "orders") as url:
        require(isinstance(parse_json(http(url + "/orders")), list), "Orders backend smoke failed")
    with forward(context, "checkout") as url:
        http(url + "/health")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True, help="GitOps checkout environments/dev/images.yaml")
    parser.add_argument("--output", type=Path, required=True, help="GitOps checkout environments/dev/accepted.json")
    parser.add_argument("--context", required=True, help="Explicit kubectl context for retail-lab-eks")
    parser.add_argument("--operator", required=True)
    args = parser.parse_args()
    require(args.operator.strip(), "Operator identity is required")
    release = release_from_values(parse_json(args.images.read_bytes()))
    resolved = {service: runtime_digests(image) for service, image in release["images"].items()}
    inspect_runtime(args.context, release, resolved)
    smoke(args.context)
    pods = inspect_runtime(args.context, release, resolved)
    accepted = {"schema_version": 1, "environment": "dev", "source_commit": release["source_commit"],
                "release_run": release["run"], "images": {service: image["reference"] for service, image in release["images"].items()},
                "checks": {"ready": True, "image_identity": True, "smoke": True},
                "checked_at": datetime.now(timezone.utc).isoformat(), "operator": args.operator, "pods": pods}
    validate_acceptance(accepted, release, release["source_commit"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json_text(accepted), encoding="utf-8")
    print("Dev acceptance recorded: " + str(args.output))
    print("Review and commit this evidence in the GitOps repository before production promotion.")


if __name__ == "__main__":
    main()

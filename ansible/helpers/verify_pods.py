#!/usr/bin/env python3
"""Check the runtime image IDs of already-read dev pods; never contact a cluster."""

import json
import re
import sys

SERVICES = {"ui", "catalog", "cart", "checkout", "orders"}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(payload):
    images = payload["images"]
    require(set(images) == SERVICES, "Expected exactly five dev images")
    results = payload["results"]
    require(len(results) == len(SERVICES) and {result["item"] for result in results} == SERVICES,
            "Expected pod observations for all five dev services")
    counts = {}
    for result in results:
        service = result["item"]
        image = images[service]
        require(DIGEST.fullmatch(image["digest"]) is not None, f"Invalid registry digest for {service}")
        require(DIGEST.fullmatch(image.get("image_id", "")) is not None, f"Missing CI image ID for {service}")
        require(image["reference"] == image["repository"] + "@" + image["digest"], f"Invalid image reference for {service}")
        pods = [pod for pod in result["resources"] if not pod.get("metadata", {}).get("deletionTimestamp")]
        require(bool(pods), f"No live pods for {service}")
        for pod in pods:
            name = pod["metadata"]["name"]
            require(pod.get("status", {}).get("phase") == "Running", f"Pod {name} is not Running")
            matching = [container for container in pod["spec"]["containers"] if container["image"] == image["reference"]]
            require(len(matching) == 1, f"Pod {name} does not have exactly one expected {service} container")
            statuses = [status for status in pod.get("status", {}).get("containerStatuses", [])
                        if status.get("name") == matching[0]["name"]]
            require(len(statuses) == 1 and statuses[0].get("ready") is True,
                    f"Pod {name} has no ready runtime status for {service}")
            runtime_id = statuses[0].get("imageID", "")
            # Kubernetes/CRI runtimes report either a pullable manifest digest or
            # the image configuration ID. Both must match immutable CI evidence.
            digest = runtime_id.rsplit("@", 1)[-1].rsplit("://", 1)[-1]
            require(DIGEST.fullmatch(digest) is not None and digest in {image["digest"], image["image_id"]},
                    f"Pod {name} runtime image ID differs from the CI release for {service}")
        counts[service] = len(pods)
    return {"verified_pods": counts, "scope": "readiness-and-runtime-image-identity"}


def main():
    try:
        print(json.dumps(verify(json.load(sys.stdin))))
    except (ValueError, KeyError, TypeError) as error:
        print("Pod verification failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

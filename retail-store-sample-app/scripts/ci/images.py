#!/usr/bin/env python3
"""Build, verify, scan and publish one immutable five-image set."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile

SERVICES = {"ui": "devops-retails-shop", "catalog": "devops-retails-shop-catalog",
            "cart": "devops-retails-shop-cart", "checkout": "devops-retails-shop-checkout",
            "orders": "devops-retails-shop-orders"}
PLATFORM = "linux/amd64"
APP_ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
HEX256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class ContractError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ContractError(message)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key: " + key)
        result[key] = value
    return result


def parse_json(value):
    return json.loads(value, object_pairs_hook=unique_keys)


def regular_file(path):
    require(not path.is_symlink() and path.is_file(), "Expected a regular file: " + str(path))
    return path


def read_json(path):
    return parse_json(regular_file(path).read_text(encoding="utf-8"))


def sha256_file(path):
    digest = hashlib.sha256()
    with regular_file(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                        dir=path.parent, prefix="." + path.name, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def run(arguments, *, capture=False, env=None):
    print("+ " + " ".join(str(arg) for arg in arguments), flush=True)
    completed = subprocess.run([str(arg) for arg in arguments], cwd=APP_ROOT, env=env,
                               check=True, text=True,
                               stdout=subprocess.PIPE if capture else None)
    return completed.stdout if capture else ""


def context():
    source = os.environ.get("GITHUB_SHA", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    namespace = os.environ.get("DOCKERHUB_NAMESPACE", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    require(COMMIT.fullmatch(source), "GITHUB_SHA must be a full commit SHA")
    require(re.fullmatch(r"[1-9][0-9]*", run_id), "Invalid GITHUB_RUN_ID")
    require(re.fullmatch(r"[1-9][0-9]*", attempt), "Invalid GITHUB_RUN_ATTEMPT")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "Invalid GITHUB_REPOSITORY")
    require(namespace == "hmannnguye", "DOCKERHUB_NAMESPACE must match the agreed hmannnguye mapping")
    require(server.startswith("https://"), "GITHUB_SERVER_URL must use HTTPS")
    return {"schema_version": 1, "source_commit": source,
            "run": {"id": run_id, "attempt": attempt,
                    "url": f"{server}/{repository}/actions/runs/{run_id}/attempts/{attempt}"},
            "platform": PLATFORM, "backend_profile": "upstream"}


def tag_for(expected):
    return f"sha-{expected['source_commit']}-run-{expected['run']['id']}-{expected['run']['attempt']}"


def repository_for(service):
    return f"docker.io/{os.environ['DOCKERHUB_NAMESPACE']}/{SERVICES[service]}"


def assert_checkout(expected):
    actual = run(["git", "rev-parse", "HEAD"], capture=True).strip()
    require(actual == expected["source_commit"], "Checkout differs from expected source commit")


def assert_executor():
    package_dir = APP_ROOT / "node_modules/@nx-tools/nx-container"
    require(read_json(package_dir / "package.json").get("version") == "6.8.2",
            "Expected lockfile-pinned nx-container 6.8.2")
    executor = read_json(package_dir / "executors.json")["executors"]["build"]
    schema_path = (package_dir / executor["schema"]).resolve()
    require(schema_path.is_relative_to(package_dir.resolve()), "Executor schema escaped its package")
    properties = read_json(schema_path)["properties"]
    for name, kind in (("load", "boolean"), ("push", "boolean"), ("platforms", "array"), ("tags", "array")):
        definition = properties.get(name, {})
        allowed = [definition.get("type")] + [item.get("type") for item in definition.get("anyOf", [])]
        require(kind in allowed, "Unsupported executor option: " + name)


def config_id_from_inspect(info):
    """Normalize legacy config IDs and Docker 29 containerd manifest IDs."""
    native_id = info.get("Id", "")
    require(isinstance(native_id, str) and DIGEST.fullmatch(native_id), "Invalid Docker image identifier")
    descriptor = info.get("Descriptor") or {}
    annotations = descriptor.get("annotations") or {}
    image_id = annotations.get("config.digest", native_id)
    require(isinstance(image_id, str) and DIGEST.fullmatch(image_id), "Invalid image config ID")
    if image_id != native_id:
        require(descriptor.get("digest") == native_id,
                "Docker manifest identifier differs from its inspected descriptor")
    return image_id


def inspect_image(reference, expected_id=None):
    info = parse_json(run(["docker", "image", "inspect", "--format", "{{json .}}", reference], capture=True))
    require(info.get("Os") == "linux" and info.get("Architecture") == "amd64", "Unexpected image platform")
    image_id = config_id_from_inspect(info)
    if expected_id is not None:
        require(image_id == expected_id, "Image config ID mismatch: " + reference)
    return image_id


def archive_config(path, image):
    """Read archive config without extracting untrusted paths."""
    with tarfile.open(regular_file(path), mode="r:*") as archive:
        members = archive.getmembers()

        def read_member(name, limit):
            matches = [member for member in members if member.name == name]
            require(len(matches) == 1, "Missing/duplicate archive member: " + name)
            member = matches[0]
            require(member.isfile() and member.size <= limit, "Invalid archive member: " + name)
            with archive.extractfile(member) as stream:
                return stream.read(limit + 1)

        manifest = parse_json(read_member("manifest.json", 1024 * 1024))
        require(isinstance(manifest, list) and len(manifest) == 1, "Archive must contain exactly one image")
        entry = manifest[0]
        tags = entry.get("RepoTags") or []
        require(isinstance(tags, list) and all(isinstance(tag, str) for tag in tags)
                and {tag.removeprefix("docker.io/") for tag in tags}
                == {image["local_tag"].removeprefix("docker.io/")}, "Archive tag differs from metadata")
        config_name = entry.get("Config", "")
        require(isinstance(config_name, str) and bool(config_name), "Missing archive config")
        config_path = PurePosixPath(config_name)
        require(not config_path.is_absolute() and ".." not in config_path.parts, "Unsafe archive path")
        config_bytes = read_member(config_name, 16 * 1024 * 1024)
        require(hmac.compare_digest("sha256:" + hashlib.sha256(config_bytes).hexdigest(), image["image_id"]),
                "Archive config differs from recorded image ID")
        config = parse_json(config_bytes)
        require(config.get("os") == "linux" and config.get("architecture") == "amd64", "Wrong archive platform")


def validate_metadata(metadata, expected):
    require(isinstance(metadata, dict) and set(metadata) == set(expected) | {"images"},
            "Unexpected/missing metadata fields")
    for field, value in expected.items():
        require(metadata.get(field) == value, "Metadata does not match current run: " + field)
    images = metadata.get("images")
    require(isinstance(images, dict) and set(images) == set(SERVICES), "Exactly five services are required")
    fields = {"repository", "tag", "local_tag", "archive", "archive_sha256", "image_id"}
    for service, image in images.items():
        require(isinstance(image, dict) and set(image) == fields, "Invalid metadata: " + service)
        repository, tag = repository_for(service), tag_for(expected)
        require(image["repository"] == repository and image["tag"] == tag, "Wrong repository/tag: " + service)
        require(image["local_tag"] == f"{repository}:{tag}", "Wrong local image tag: " + service)
        require(image["archive"] == service + ".tar", "Wrong archive path")
        require(isinstance(image["archive_sha256"], str) and HEX256.fullmatch(image["archive_sha256"]),
                "Invalid archive checksum")
        require(isinstance(image["image_id"], str) and DIGEST.fullmatch(image["image_id"]), "Invalid image ID")
    return metadata


def verify(directory, *, load=False):
    metadata = validate_metadata(read_json(directory / "metadata.json"), context())
    for service in SERVICES:
        image = metadata["images"][service]
        path = regular_file(directory / image["archive"])
        require(hmac.compare_digest(sha256_file(path), image["archive_sha256"]),
                "Archive checksum mismatch: " + service)
        archive_config(path, image)
    if load:
        for image in metadata["images"].values():
            run(["docker", "image", "load", "--input", directory / image["archive"]])
            inspect_image(image["local_tag"], image["image_id"])
    return metadata


def build(directory):
    expected = context()
    assert_checkout(expected)
    assert_executor()
    directory.mkdir(parents=True, exist_ok=True)
    require(not (directory / "metadata.json").exists(), "Refusing to overwrite an existing image set")
    require(not any((directory / (service + ".tar")).exists() for service in SERVICES), "Image directory must be fresh")
    metadata = dict(expected, images={})
    for service in SERVICES:
        repository, tag = repository_for(service), tag_for(expected)
        local_tag = f"{repository}:{tag}"
        build_env = {key: value for key, value in os.environ.items() if not key.startswith("INPUT_")}
        build_env.update({"NX_DAEMON": "false", "NX_SKIP_NX_CACHE": "true",
                          "INPUT_PUSH": "false", "INPUT_LOAD": "true", "INPUT_PLATFORMS": PLATFORM,
                          "INPUT_TAGS": local_tag, "INPUT_PROVENANCE": "false", "INPUT_SBOM": "false"})
        run(["yarn", "nx", "run", service + ":container", "--skip-nx-cache", "--load=true",
             "--push=false", "--platforms=" + PLATFORM, "--tags=" + local_tag,
             "--provenance=false", "--sbom=false"], env=build_env)
        image_id = inspect_image(local_tag)
        archive_path = directory / (service + ".tar")
        run(["docker", "image", "save", "--output", archive_path, local_tag])
        image = {"repository": repository, "tag": tag, "local_tag": local_tag,
                 "archive": archive_path.name, "archive_sha256": sha256_file(archive_path), "image_id": image_id}
        archive_config(archive_path, image)
        metadata["images"][service] = image
    validate_metadata(metadata, expected)
    write_json_atomic(directory / "metadata.json", metadata)
    verify(directory)


def scan(directory):
    metadata = verify(directory)
    version = os.environ.get("TRIVY_VERSION", "").removeprefix("v")
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version), "TRIVY_VERSION must be pinned")
    require(re.search(rf"(?m)^Version:\s*{re.escape(version)}\s*$", run(["trivy", "--version"], capture=True)),
            "Installed scanner differs from pinned version")
    reports = directory / "scan"
    reports.mkdir(parents=True, exist_ok=True)
    results, failures = {}, []
    for service, image in metadata["images"].items():
        try:
            run(["trivy", "image", "--input", directory / image["archive"], "--scanners", "vuln",
                 "--pkg-types", "os,library", "--severity", "HIGH,CRITICAL", "--exit-code", "1",
                 "--timeout", "10m", "--format", "json", "--output", reports / (service + ".json")])
            results[service] = {"status": "passed", "exit_code": 0}
        except (OSError, subprocess.CalledProcessError) as error:
            failures.append(service)
            results[service] = {"status": "failed", "exit_code": getattr(error, "returncode", None)}
            print("Trivy failed for " + service + ": " + str(error), file=sys.stderr)
        results[service].update(archive_sha256=image["archive_sha256"], image_id=image["image_id"])
    write_json_atomic(reports / "summary.json", {"source_commit": metadata["source_commit"], "run": metadata["run"],
                                               "trivy_version": version, "images": results})
    require(not failures, "Trivy gate failed: " + ", ".join(failures))


def remote_digest(reference):
    description = run(["docker", "buildx", "imagetools", "inspect", reference], capture=True)
    matches = re.findall(r"(?m)^Digest:\s*(sha256:[0-9a-f]{64})\s*$", description)
    require(len(matches) == 1, "Cannot resolve registry digest: " + reference)
    return matches[0]


def validate_release(release, expected):
    require(isinstance(release, dict) and set(release) == set(expected) | {"images"}, "Invalid release fields")
    for field, value in expected.items():
        require(release.get(field) == value, "Invalid release: " + field)
    require(isinstance(release.get("images"), dict) and set(release["images"]) == set(SERVICES),
            "Complete release requires five services")
    for service, image in release["images"].items():
        require(set(image) == {"repository", "tag", "digest", "reference", "image_id"}, "Invalid release image fields")
        repository = repository_for(service)
        require(image["repository"] == repository and image["tag"] == tag_for(expected), "Wrong release mapping")
        require(isinstance(image["digest"], str) and DIGEST.fullmatch(image["digest"]), "Invalid registry digest")
        require(image["reference"] == repository + "@" + image["digest"], "Invalid digest reference")
        require(isinstance(image["image_id"], str) and DIGEST.fullmatch(image["image_id"]), "Invalid config ID")
    return release


def publish(directory):
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Publish is restricted to main")
    require(os.environ.get("GITHUB_EVENT_NAME") in {"push", "workflow_dispatch"}, "Untrusted publish event")
    expected = context()
    assert_checkout(expected)
    output = directory / "images.json"
    require(not output.exists(), "Refusing to reuse an existing release manifest")
    metadata = verify(directory, load=True)
    release = dict(expected, images={})
    for service, image in metadata["images"].items():
        inspect_image(image["local_tag"], image["image_id"])
        # The verified archive already carries the final immutable run tag.
        # Config IDs are not taggable image identifiers on every Docker store.
        run(["docker", "image", "push", image["local_tag"]])
        digest = remote_digest(image["local_tag"])
        reference = image["repository"] + "@" + digest
        run(["docker", "image", "pull", "--platform", PLATFORM, reference])
        inspect_image(reference, image["image_id"])
        release["images"][service] = {"repository": image["repository"], "tag": image["tag"],
                                      "digest": digest, "reference": reference, "image_id": image["image_id"]}
    validate_release(release, expected)
    write_json_atomic(output, release)
    print("Complete release manifest: " + str(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify", "scan", "publish"))
    parser.add_argument("--directory", type=Path, default=Path("dist/ci/images"))
    parser.add_argument("--load", action="store_true")
    args = parser.parse_args()
    require(not args.load or args.command == "verify", "--load is supported only by verify")
    directory = args.directory.resolve()
    if args.command == "verify":
        verify(directory, load=args.load)
    else:
        {"build": build, "scan": scan, "publish": publish}[args.command](directory)


if __name__ == "__main__":
    try:
        main()
    except (ContractError, OSError, KeyError, TypeError, json.JSONDecodeError,
            tarfile.TarError, subprocess.CalledProcessError) as error:
        print("Image pipeline failed: " + str(error), file=sys.stderr)
        sys.exit(1)

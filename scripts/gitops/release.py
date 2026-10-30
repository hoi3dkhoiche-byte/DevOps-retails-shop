#!/usr/bin/env python3
"""Consume a verified CI release and propose immutable GitOps image updates."""
import argparse
import base64
import copy
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

SOURCE_REPO = "hoi3dkhoiche-byte/DevOps-retails-shop"
GITOPS_REPO = "hoi3dkhoiche-byte/DevOps-retails-shop-gitops"
PROFILE = "mysql-redis-opensearch"
SERVICES = {name: "docker.io/hmannnguye/devops-retails-shop" + ("" if name == "ui" else "-" + name)
            for name in ("ui", "catalog", "cart", "checkout", "orders")}
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
POSITIVE = re.compile(r"[1-9][0-9]*\Z")
REQUIRED_JOBS = {"plan", "style", "images", "scan", "ci-required", "publish",
                 *("source-" + name for name in SERVICES), "e2e-compose", "e2e-search", "e2e-kind"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key: " + key)
        result[key] = value
    return result


def parse_json(value):
    return json.loads(value, object_pairs_hook=unique_keys)


def json_text(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def validate_release(value, run_id=None, attempt=None, source_commit=None):
    fields = {"schema_version", "source_commit", "run", "platform", "backend_profile", "images"}
    require(isinstance(value, dict) and set(value) == fields, "Invalid release fields")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, "Unsupported release schema")
    require(isinstance(value["source_commit"], str) and COMMIT.fullmatch(value["source_commit"]), "Invalid source commit")
    require(value["platform"] == "linux/amd64" and value["backend_profile"] == PROFILE, "Unaccepted platform/backend profile")
    run = value["run"]
    require(isinstance(run, dict) and set(run) == {"id", "attempt", "url"}, "Invalid run provenance")
    require(all(isinstance(run.get(key), str) and POSITIVE.fullmatch(run[key]) for key in ("id", "attempt")), "Invalid run ID/attempt")
    require(run["url"] == f"https://github.com/{SOURCE_REPO}/actions/runs/{run['id']}/attempts/{run['attempt']}", "Wrong release run URL")
    for actual, expected, name in ((run["id"], run_id, "run ID"), (run["attempt"], attempt, "run attempt"),
                                   (value["source_commit"], source_commit, "source commit")):
        require(expected is None or actual == str(expected), "Stale release " + name)
    require(isinstance(value["images"], dict) and set(value["images"]) == set(SERVICES), "A release requires all five images")
    tag = f"sha-{value['source_commit']}-run-{run['id']}-{run['attempt']}"
    for name, repository in SERVICES.items():
        image = value["images"][name]
        require(isinstance(image, dict) and set(image) == {"repository", "tag", "digest", "reference", "image_id"}, "Invalid image fields: " + name)
        require(image["repository"] == repository and image["tag"] == tag, "Wrong image mapping/tag: " + name)
        require(all(isinstance(image[key], str) and DIGEST.fullmatch(image[key]) for key in ("digest", "image_id")), "Invalid image digest: " + name)
        require(image["reference"] == repository + "@" + image["digest"], "Wrong digest reference: " + name)
    return value


def image_values(release):
    validate_release(release)
    metadata = {key: copy.deepcopy(value) for key, value in release.items() if key != "images"}
    return {"release": dict(enabled=True, **metadata), "images": copy.deepcopy(release["images"])}


def release_from_values(values, allow_disabled=False):
    require(isinstance(values, dict) and set(values) == {"release", "images"}, "Invalid GitOps image values")
    metadata = values.get("release")
    require(isinstance(metadata, dict) and type(metadata.get("enabled")) is bool, "Missing release enabled flag")
    if not metadata["enabled"]:
        require(allow_disabled and not values["images"], "GitOps environment has no enabled release")
        return None
    return validate_release({**{key: value for key, value in metadata.items() if key != "enabled"}, "images": values["images"]})


def order(release):
    return (int(release["run"]["id"]), int(release["run"]["attempt"]))


def validate_attempt(run, jobs, expected_id, expected_attempt, expected_sha):
    require(run.get("id") == int(expected_id) and run.get("run_attempt") == int(expected_attempt), "Wrong CI attempt")
    require(run.get("name") == "Retail CI" and run.get("path") == ".github/workflows/ci.yml", "Untrusted source workflow")
    require(run.get("repository", {}).get("full_name") == SOURCE_REPO
            and run.get("head_repository", {}).get("full_name") == SOURCE_REPO, "Untrusted source repository")
    require(run.get("head_branch") == "main" and run.get("head_sha") == expected_sha
            and run.get("event") in {"push", "workflow_dispatch"}, "Release must come from trusted main")
    require(run.get("status") == "completed" and run.get("conclusion") == "success", "Source CI did not succeed")
    names = [job.get("name") for job in jobs]
    require(len(names) == len(set(names)), "Duplicate CI job names")
    require(REQUIRED_JOBS <= set(names), "CI attempt omitted required jobs; rerun all jobs")
    for job in jobs:
        if job.get("name") in REQUIRED_JOBS:
            require(job.get("status") == "completed" and job.get("conclusion") == "success", "Required CI job did not pass: " + job["name"])
            require(job.get("run_id") == int(expected_id) and job.get("run_attempt") == int(expected_attempt),
                    "Required CI job belongs to another run/attempt: " + job["name"])
    return run


def validate_acceptance(accepted, release, expected_commit):
    validate_release(release, source_commit=expected_commit)
    require(isinstance(accepted, dict) and type(accepted.get("schema_version")) is int and accepted["schema_version"] == 1
            and accepted.get("environment") == "dev", "Missing dev acceptance proof")
    require(accepted.get("source_commit") == expected_commit and accepted.get("release_run") == release["run"], "Stale dev acceptance provenance")
    references = {service: image["reference"] for service, image in release["images"].items()}
    require(accepted.get("images") == references, "Dev acceptance does not cover the exact five digests")
    checks = accepted.get("checks")
    require(isinstance(checks, dict) and set(checks) == {"ready", "image_identity", "smoke"}
            and all(value is True for value in checks.values()), "Dev acceptance checks did not pass")
    require(isinstance(accepted.get("checked_at"), str) and isinstance(accepted.get("operator"), str)
            and accepted["operator"].strip() and accepted.get("pods"), "Incomplete runtime acceptance evidence")
    require(datetime.fromisoformat(accepted["checked_at"]).utcoffset() is not None, "Acceptance timestamp must include its timezone")
    pods = accepted["pods"]
    require(isinstance(pods, dict) and set(pods) == set(SERVICES), "Runtime evidence must cover all five services")
    for service, records in pods.items():
        require(isinstance(records, list) and bool(records), "No accepted pod for " + service)
        for pod in records:
            require(isinstance(pod, dict) and isinstance(pod.get("name"), str) and pod["name"]
                    and pod.get("ready") is True and pod.get("image_reference") == references[service]
                    and pod.get("verified_config_digest") == release["images"][service]["image_id"]
                    and isinstance(pod.get("runtime_image_id"), str)
                    and re.search(r"sha256:[0-9a-f]{64}$", pod["runtime_image_id"]), "Invalid runtime identity proof: " + service)
    return release


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub artifact redirects must never forward the API token to blob storage."""
    def redirect_request(self, request, response, code, message, headers, new_url):
        require(urllib.parse.urlsplit(new_url).scheme == "https", "Refusing non-HTTPS redirect")
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        if redirected is not None and urllib.parse.urlsplit(new_url).netloc != urllib.parse.urlsplit(request.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


class GitHub:
    def __init__(self, token, repository):
        require(bool(token), "GitHub token is required")
        require(repository in {SOURCE_REPO, GITOPS_REPO}, "Unexpected repository")
        self.token = token
        self.repository = repository
        self.opener = urllib.request.build_opener(SafeRedirect())

    def request(self, method, path, payload=None, missing_ok=False, binary=False):
        require(path.startswith("/") and not path.startswith("//"), "Invalid API path")
        url = "https://api.github.com/repos/" + self.repository + path
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "retail-gitops-release", "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read(20 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            raise RuntimeError(f"GitHub {method} {path.split('?')[0]} returned HTTP {error.code}") from None
        require(len(raw) <= 20 * 1024 * 1024, "GitHub response exceeds the allowed size")
        return raw if binary else parse_json(raw) if raw else None

    def pages(self, path, key):
        result = []
        for page in range(1, 101):
            separator = "&" if "?" in path else "?"
            value = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            items = value[key] if key else value
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError("GitHub pagination exceeded the allowed limit")

    def file(self, path, ref="main", missing_ok=False):
        require(not path.startswith("/") and ".." not in path.split("/"), "Invalid repository file path")
        item = self.request("GET", "/contents/" + urllib.parse.quote(path, safe="/") + "?ref=" + urllib.parse.quote(ref, safe=""), missing_ok=missing_ok)
        if item is None:
            return None
        require(item.get("type") == "file" and item.get("encoding") == "base64", "Expected a UTF-8 repository file")
        return parse_json(base64.b64decode(item["content"]))


def read_archive(archive, expected_digest):
    require(isinstance(expected_digest, str) and DIGEST.fullmatch(expected_digest), "Artifact checksum is required")
    require("sha256:" + hashlib.sha256(archive).hexdigest() == expected_digest, "Release artifact checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        require(bundle.namelist() == ["images.json", "images.schema.json"]
                or bundle.namelist() == ["images.schema.json", "images.json"], "Unexpected release archive entries")
        require(all(item.file_size <= 1024 * 1024 for item in bundle.infolist()), "Oversized release manifest")
        release = parse_json(bundle.read("images.json"))
        schema = parse_json(bundle.read("images.schema.json"))
    return release, schema


def ci_release(source, event):
    payload = event.get("workflow_run", {})
    run_id, attempt, sha = payload.get("id"), payload.get("run_attempt"), payload.get("head_sha")
    require(type(run_id) is int and type(attempt) is int and run_id > 0 and attempt > 0
            and isinstance(sha, str) and COMMIT.fullmatch(sha), "Invalid triggering CI run")
    require(payload.get("head_repository", {}).get("full_name") == SOURCE_REPO
            and payload.get("head_branch") == "main" and payload.get("conclusion") == "success", "Untrusted workflow_run event")
    run = source.request("GET", f"/actions/runs/{run_id}/attempts/{attempt}")
    jobs = source.pages(f"/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    validate_attempt(run, jobs, run_id, attempt, sha)
    artifacts = source.pages(f"/actions/runs/{run_id}/artifacts", "artifacts")
    matches = [item for item in artifacts if item.get("name") == f"gitops-images-{run_id}-{attempt}" and not item.get("expired")]
    require(len(matches) == 1, "Missing/ambiguous release artifact for this exact CI attempt")
    artifact = matches[0]
    require(artifact.get("workflow_run", {}).get("id") == run_id
            and artifact.get("workflow_run", {}).get("head_sha") == sha, "Artifact source does not match CI")
    archive = source.request("GET", f"/actions/artifacts/{artifact['id']}/zip", binary=True)
    release, schema = read_archive(archive, artifact.get("digest"))
    require(schema == source.file("retail-store-sample-app/scripts/ci/images.schema.json", ref=sha), "Artifact schema differs from its source commit")
    return validate_release(release, run_id, attempt, sha)


def propose(gitops, environment, release):
    require(environment in {"dev", "prod"}, "Invalid target environment")
    validate_release(release)
    path = f"environments/{environment}/images.yaml"
    current = gitops.file(path, missing_ok=True)
    require(current is not None, "Initialize the GitOps main branch from gitops/ before enabling automation")
    current_release = release_from_values(current, allow_disabled=True)
    if current_release is not None and order(current_release) >= order(release):
        print(f"Skip {environment}: an equal or newer CI release is already merged.")
        return None
    prefix = f"automation/retail-{environment}-images-"
    marker = "<!-- retail-gitops automation v1 -->"
    generated = []
    for pull in gitops.pages("/pulls?state=open&base=main", None):
        head = pull.get("head", {})
        if (head.get("repo") or {}).get("full_name") != GITOPS_REPO or not head.get("ref", "").startswith(prefix):
            continue
        if not (pull.get("body") or "").startswith(marker):
            continue
        pending = release_from_values(gitops.file(path, ref=head["ref"]))
        if order(pending) >= order(release):
            print(f"Skip {environment}: an equal or newer CI release already has PR {pull['number']}.")
            return pull
        generated.append(pull)
    branch = prefix + release["run"]["id"] + "-" + release["run"]["attempt"]
    main = gitops.request("GET", "/git/ref/heads/main")["object"]["sha"]
    reference = gitops.request("GET", "/git/ref/heads/" + branch, missing_ok=True)
    if reference:
        # Recover if a previous invocation created this branch but PR creation failed.
        require(gitops.file(path, ref=branch) == image_values(release), "Existing automation branch contains another release")
        comparison = gitops.request("GET", "/compare/" + main + "..." + reference["object"]["sha"])
        require(all(item.get("filename") == path for item in comparison.get("files", [])), "Automation branch contains other changes; preserve them for manual review")
    title = f"chore({environment}): deploy {release['source_commit'][:12]} (CI {release['run']['id']}/{release['run']['attempt']})"
    if not reference:
        base = gitops.request("GET", "/git/commits/" + main)
        tree = gitops.request("POST", "/git/trees", {"base_tree": base["tree"]["sha"], "tree": [{
            "path": path, "mode": "100644", "type": "blob", "content": json_text(image_values(release))}]})
        commit = gitops.request("POST", "/git/commits", {"message": title, "tree": tree["sha"], "parents": [main]})
        gitops.request("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": commit["sha"]})
    body = marker + f"\nDeploy the complete five-image release from [{release['source_commit'][:12]}]({release['run']['url']}).\n\n"
    body += "Images use immutable registry digests and the mysql-redis-opensearch backend profile.\n\n"
    body += ("Merge to let Argo CD sync dev. Run the dev acceptance check before promoting this release.\n" if environment == "dev"
             else "This is the exact image set accepted in dev. Merge, then sync production and manually promote the UI Rollout.\n")
    pull = gitops.request("POST", "/pulls", {"title": title, "body": body, "head": branch, "base": "main"})
    for previous in generated:
        latest = gitops.request("GET", f"/pulls/{previous['number']}")
        if latest.get("state") != "open" or not (latest.get("body") or "").startswith(marker) \
                or latest.get("head", {}).get("sha") != previous.get("head", {}).get("sha"):
            print(f"Preserve PR {previous['number']}: it changed while the release was being proposed.")
            continue
        comparison = gitops.request("GET", "/compare/" + main + "..." + latest["head"]["sha"])
        if all(item.get("filename") == path for item in comparison.get("files", [])):
            gitops.request("PATCH", f"/pulls/{previous['number']}", {"state": "closed"})
        else:
            print(f"Preserve PR {previous['number']}: it contains additional changes.")
    print("GitOps PR: " + pull["html_url"])
    return pull


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    dev = commands.add_parser("dev")
    dev.add_argument("--event", type=Path, default=Path(os.environ.get("GITHUB_EVENT_PATH", "event.json")))
    prod = commands.add_parser("prod")
    prod.add_argument("--expected-dev-commit", required=True)
    args = parser.parse_args()
    gitops = GitHub(os.environ.get("GITOPS_TOKEN", ""), GITOPS_REPO)
    if args.command == "dev":
        source = GitHub(os.environ.get("SOURCE_TOKEN", ""), SOURCE_REPO)
        release = ci_release(source, parse_json(args.event.read_bytes()))
        propose(gitops, "dev", release)
    else:
        require(COMMIT.fullmatch(args.expected_dev_commit), "Expected dev commit must be a full SHA")
        release = release_from_values(gitops.file("environments/dev/images.yaml"))
        validate_acceptance(gitops.file("environments/dev/accepted.json"), release, args.expected_dev_commit)
        propose(gitops, "prod", release)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, zipfile.BadZipFile) as error:
        print("GitOps update failed: " + str(error), file=sys.stderr)
        sys.exit(1)

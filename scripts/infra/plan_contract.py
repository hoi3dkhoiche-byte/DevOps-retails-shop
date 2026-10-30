#!/usr/bin/env python3
"""Bind a saved Terraform plan to its trusted source run and reviewed checksum."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request

REPOSITORY = "hoi3dkhoiche-byte/DevOps-retails-shop"
WORKFLOW = ".github/workflows/terraform.yml"
VERSION = "1.17.1"


def checksum(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def github(path):
    request = urllib.request.Request("https://api.github.com/repos/" + REPOSITORY + path,
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def verify(metadata, plan, expected_commit, approved_hash, run, jobs, expected_role, now=None):
    require(set(metadata) == {"schema_version", "source_commit", "run_id", "run_attempt", "repository", "terraform_version", "created_at", "sha256", "role_arn", "aws_account_id"}, "Malformed plan metadata")
    require(metadata["schema_version"] == 1 and metadata["repository"] == REPOSITORY, "Untrusted plan repository/schema")
    require(re.fullmatch(r"[0-9a-f]{40}", metadata["source_commit"]) and metadata["source_commit"] == expected_commit, "Plan source differs from the checked-out main commit; create a fresh plan")
    require(metadata["terraform_version"] == VERSION, "Plan Terraform version differs")
    role = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/.+", expected_role)
    require(role and metadata["role_arn"] == expected_role and metadata["aws_account_id"] == role.group(1), "Plan AWS role/account differs from the apply target")
    require(all(re.fullmatch(r"[1-9][0-9]*", str(metadata[field])) for field in ("run_id", "run_attempt")), "Invalid run identity")
    require(re.fullmatch(r"[0-9a-f]{64}", approved_hash) and metadata["sha256"] == approved_hash == checksum(plan), "Reviewed checksum does not match the downloaded plan")
    created = datetime.fromisoformat(metadata["created_at"])
    age = ((now or datetime.now(timezone.utc)) - created).total_seconds()
    require(0 <= age <= 86400, "Plan must have been created within the last 24 hours")
    require(str(run.get("id")) == str(metadata["run_id"]) and str(run.get("run_attempt")) == str(metadata["run_attempt"]), "Wrong or superseded plan attempt")
    require(run.get("head_sha") == expected_commit and run.get("head_branch") == "main", "Plan must originate on the same main commit")
    require(run.get("event") == "workflow_dispatch" and run.get("conclusion") == "success" and run.get("path") == WORKFLOW, "Plan run was not a successful manual Terraform workflow")
    results = {job["name"]: job.get("conclusion") for job in jobs}
    require(len(results) == len(jobs), "Duplicate workflow job identities")
    require(results.get("validate") == "success" and results.get("plan") == "success" and results.get("apply") in (None, "skipped"), "Required plan jobs did not pass or this run already applied")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["record", "verify"])
    parser.add_argument("--directory", type=Path, default=Path("infra/retail-lab/reviewed-plan"))
    args = parser.parse_args()
    directory = args.directory
    plan = directory / "terraform.tfplan"
    path = directory / "metadata.json"
    if args.operation == "record":
        metadata = {"schema_version": 1, "source_commit": os.environ["GITHUB_SHA"], "run_id": os.environ["GITHUB_RUN_ID"],
            "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"], "repository": REPOSITORY, "terraform_version": VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(), "sha256": checksum(plan),
            "role_arn": os.environ["TERRAFORM_ROLE_ARN"], "aws_account_id": os.environ["AWS_ACCOUNT_ID"]}
        path.write_text(json.dumps(metadata, indent=2) + "\n")
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("\nSaved Terraform plan for `" + metadata["source_commit"] + "`.\n\nReview `terraform-plan.txt` in the artifact, then run **apply** with:\n\n"
                "- Plan run ID: `" + metadata["run_id"] + "`\n- Plan attempt: `" + metadata["run_attempt"] + "`\n- Approved SHA256: `" + metadata["sha256"] + "`\n")
        return
    metadata = json.loads(path.read_text())
    require(str(metadata["run_id"]) == os.environ["PLAN_RUN_ID"] and str(metadata["run_attempt"]) == os.environ["PLAN_ATTEMPT"], "Downloaded artifact differs from requested run/attempt")
    run = github("/actions/runs/" + os.environ["PLAN_RUN_ID"])
    jobs = github("/actions/runs/" + os.environ["PLAN_RUN_ID"] + "/attempts/" + os.environ["PLAN_ATTEMPT"] + "/jobs?per_page=100")["jobs"]
    require(github("/git/ref/heads/main")["object"]["sha"] == os.environ["GITHUB_SHA"], "Main advanced after this apply was dispatched; create a fresh plan")
    verify(metadata, plan, os.environ["GITHUB_SHA"], os.environ["APPROVED_SHA256"], run, jobs, os.environ["TERRAFORM_ROLE_ARN"])
    print("Reviewed Terraform plan source, run, attempt, age and checksum verified.")


if __name__ == "__main__":
    main()

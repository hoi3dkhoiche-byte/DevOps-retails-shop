#!/usr/bin/env python3
"""The stable required check: unexpected skipped/cancelled/failed jobs cannot pass."""
import argparse
import json
import os
from pathlib import Path
import sys
from checks import verify as verify_source
from e2e import junit_results
from images import SERVICES, context, read_json, require, validate_metadata
from plan import select_paths


def validate_jobs(needs, plan):
    expected = {"plan": "success", "style": "success"}
    for job in ("source", "images", "e2e", "scan"):
        expected[job] = "success" if plan["any_source"] else "skipped"
    for job, status in expected.items():
        require(needs.get(job, {}).get("result") == status,
                f"Required job {job}: expected {status}, got {needs.get(job, {}).get('result')}")


def validate_plan(plan):
    selected = plan.get("services")
    require(plan.get("schema") == 1 and isinstance(selected, list)
            and len(selected) == len(set(selected)) and set(selected) <= set(SERVICES), "Invalid service selection")
    require(plan.get("sha") == os.environ["GITHUB_SHA"], "Stale plan source commit")
    require(type(plan.get("any_source")) is bool and plan["any_source"] == bool(selected), "Invalid empty selection")
    require(plan.get("run_e2e") == bool(selected), "E2E cannot skip selected app changes")
    require(set(SERVICES) <= set(plan.get("nx_projects", [])), "Nx graph was not verified")
    affected = set(plan.get("nx_affected", [])) & set(SERVICES)
    require(affected <= set(selected), "Plan omitted Nx affected services")
    classified = select_paths(plan.get("paths", []), plan["event"])
    require(set(classified["services"]) <= set(selected), "Plan omitted changed services/shared inputs")
    if plan.get("full_check") or plan["event"] != "pull_request":
        require(set(selected) == set(SERVICES), "Full validation did not select all five services")
    require(plan.get("source_matrix") == {"include": [{"service": s} for s in selected]}, "Matrix differs from selection")


def validate_e2e(evidence, metadata, mode, native):
    require(evidence.get("status") == "passed" and evidence.get("mode") == mode, "E2E did not pass: " + mode)
    require(evidence.get("source_commit") == metadata["source_commit"] and evidence.get("run") == metadata["run"],
            "Stale E2E evidence")
    require(evidence.get("backend_profile") == "mysql-redis-opensearch", "Unaccepted backend profile")
    counts = junit_results(native, mode == "search")
    recorded = evidence.get("cypress")
    require(isinstance(recorded, dict) and isinstance(recorded.get("passed_test_names"), list)
            and all(isinstance(name, str) for name in recorded["passed_test_names"]),
            "Malformed Cypress test names: " + mode)
    # Artifact extraction can change file enumeration order. Compare the same
    # test-name multiset, preserving duplicates and all numeric result counts.
    recorded = dict(recorded, passed_test_names=sorted(recorded["passed_test_names"]))
    counts["passed_test_names"] = sorted(counts["passed_test_names"])
    require(recorded == counts, "Cypress evidence disagrees with native XML: " + mode)
    identities = evidence.get("identity_checks", [])
    require(len(identities) >= 2, "Missing before/after restart identity checks")
    for snapshot in identities:
        require(set(snapshot) == set(SERVICES), "E2E identity evidence is missing a service")
        for service in SERVICES:
            actual = snapshot[service].get("config_digest" if mode == "kind" else "image_id")
            require(actual == metadata["images"][service]["image_id"], "E2E ran a different image: " + service)
    persistence = evidence.get("orders_persistence", {})
    before = persistence.get("orders_before_restart", 0)
    require(type(before) is int and before > 0
            and persistence.get("database_rows", 0) >= before
            and persistence.get("orders_after_restart", 0) >= before, "Missing order persistence proof")


def validate_scan(summary, metadata, directory):
    require(summary.get("source_commit") == metadata["source_commit"] and summary.get("run") == metadata["run"],
            "Stale scan evidence")
    require(summary.get("trivy_version") == os.environ["TRIVY_VERSION"], "Wrong scanner version")
    require(set(summary.get("images", {})) == set(SERVICES), "Scan did not cover all five images")
    for service, expected in metadata["images"].items():
        result = summary["images"][service]
        require(result.get("status") == "passed" and result.get("exit_code") == 0, "Scan failed: " + service)
        require(result.get("archive_sha256") == expected["archive_sha256"]
                and result.get("image_id") == expected["image_id"], "Scan used a different image")
        native = read_json(directory / (service + ".json"))
        require(native.get("SchemaVersion") == 2, "Missing/malformed native Trivy report")
        for group in native.get("Results") or []:
            for vulnerability in group.get("Vulnerabilities") or []:
                require(vulnerability.get("Severity") not in {"HIGH", "CRITICAL"}, "High/critical vulnerability remains")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, default=Path("reports/plan.json"))
    parser.add_argument("--reports", type=Path, default=Path("reports"))
    args = parser.parse_args()
    plan = read_json(args.plan)
    validate_plan(plan)
    validate_jobs(json.loads(os.environ["NEEDS_JSON"]), plan)
    verify_source(args.plan, args.reports / "source")
    if plan["any_source"]:
        metadata = validate_metadata(read_json(args.reports / "images/metadata.json"), context())
        for mode in ("compose", "search", "kind"):
            directory = args.reports / "e2e" / mode
            validate_e2e(read_json(directory / "evidence.json"), metadata, mode, sorted(directory.glob("junit-*.xml")))
        validate_scan(read_json(args.reports / "scan/summary.json"), metadata, args.reports / "scan")
    message = "All required checks and native evidence passed." if plan["any_source"] else "No app project affected; explicit Git/Nx selection verified. Static checks passed."
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write("\n## ci-required\n\n" + message + "\n\nBackend profile: mysql-redis-opensearch (shared MySQL 8.4, isolated service databases; Redis; OpenSearch).\n")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print("ci-required failed: " + str(error), file=sys.stderr)
        sys.exit(1)

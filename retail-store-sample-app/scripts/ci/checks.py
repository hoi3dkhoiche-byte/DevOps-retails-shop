#!/usr/bin/env python3
"""Run Nx checks and retain native evidence; zero or skipped-only suites fail."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET

APP = Path(__file__).resolve().parents[2]
SUITES = ("build", "lint", "unit", "integration")


def manifest():
    value = json.loads(Path(__file__).with_name("suites.json").read_text(encoding="utf-8"))
    if value["schema"] != 1 or set(value["services"]) != {"ui", "catalog", "cart", "checkout", "orders"}:
        raise ValueError("Invalid suite manifest")
    return value["services"]


def counts(passed=0, failed=0, skipped=0, suite_errors=0):
    return dict(tests=passed + failed + skipped, executed=passed + failed,
                passed=passed, failed=failed, skipped=skipped, suite_errors=suite_errors)


def validate_counts(value, minimum=1):
    for key in counts():
        if type(value.get(key)) is not int or value[key] < 0:
            raise ValueError("Invalid or missing test count: " + key)
    if value["tests"] != value["executed"] + value["skipped"] or value["executed"] != value["passed"] + value["failed"]:
        raise ValueError("Inconsistent test counts")
    if value["failed"] or value["suite_errors"] or value["executed"] < minimum:
        raise ValueError("Required suite failed, ran zero tests, or skipped every test")


def parse_junit(documents):
    passed = failed = skipped = errors = 0
    for document in documents:
        root = ET.fromstring(document)
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "testsuite" and (int(element.get("errors", 0)) or int(element.get("failures", 0))):
                errors += 1
            if tag == "testcase":
                children = {child.tag.rsplit("}", 1)[-1] for child in element}
                if children & {"failure", "error"}:
                    failed += 1
                elif "skipped" in children:
                    skipped += 1
                else:
                    passed += 1
    return counts(passed, failed, skipped, errors)


def parse_go(output):
    started, terminal, package_errors = set(), {}, set()
    for line in output.splitlines():
        clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        start = clean.find("{")
        if start < 0:
            continue
        try:
            event = json.loads(clean[start:])
        except json.JSONDecodeError:
            if '"Action"' in clean[start:]:
                raise ValueError("Malformed Go test JSON")
            continue
        if not isinstance(event, dict) or "Action" not in event or "Package" not in event:
            continue
        if "(cached)" in event.get("Output", ""):
            raise ValueError("Go tests were cached")
        action, test = event["Action"], event.get("Test")
        if test:
            key = (event["Package"], test)
            if action == "run":
                started.add(key)
            elif action in {"pass", "fail", "skip"}:
                if key in terminal:
                    raise ValueError("Duplicate Go test completion")
                terminal[key] = action
        elif action == "fail":
            package_errors.add(event["Package"])
    if started - terminal.keys():
        raise ValueError("Incomplete Go test evidence")
    values = list(terminal.values())
    return counts(values.count("pass"), values.count("fail"), values.count("skip"), len(package_errors))


def parse_jest(value):
    passed = failed = skipped = 0
    for suite in value.get("testResults", []):
        for case in suite.get("assertionResults", []):
            status = case.get("status")
            if status == "passed":
                passed += 1
            elif status == "failed":
                failed += 1
            elif status in {"pending", "todo", "skipped", "disabled"}:
                skipped += 1
            else:
                raise ValueError("Unknown Jest test status")
    if value.get("numTotalTests") != passed + failed + skipped:
        raise ValueError("Jest aggregate disagrees with test results")
    errors = int(value.get("numRuntimeErrorTestSuites", 0))
    if value.get("success") is not True or value.get("wasInterrupted"):
        errors += 1
    return counts(passed, failed, skipped, errors)


def validate_source_report(report, service, sha, specifications=None):
    specifications = specifications or manifest()
    if report.get("schema") != 1 or report.get("sha") != sha or report.get("service") != service:
        raise ValueError("Source evidence identity mismatch")
    checks = report.get("checks", [])
    if not isinstance(checks, list) or len(checks) != len(SUITES):
        raise ValueError("Missing source suites")
    indexed = {check.get("suite"): check for check in checks}
    if set(indexed) != set(SUITES):
        raise ValueError("Missing/duplicate source suite")
    for suite, spec in specifications[service].items():
        result = indexed[suite]
        if not spec["required"]:
            if result.get("status") != "not_applicable" or result.get("reason") != spec["reason"]:
                raise ValueError("N/A requires its declared reason")
            continue
        if result.get("status") != "passed" or result.get("returncode") != 0:
            raise ValueError("Required source check did not pass: " + suite)
        evidence = result.get("report_files")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("Required check has no evidence")
        extension = {"junit": ".xml", "jest": ".json"}.get(spec.get("format"))
        if extension and not any(Path(p).suffix == extension and "native" in Path(p).parts for p in evidence):
            raise ValueError("Required native test report missing")
        if spec.get("format"):
            validate_counts(result, spec.get("minimum_executed", 1))
    if report.get("status") != "passed":
        raise ValueError("Source job report failed")


def execute(command, stream, timeout):
    environment = dict(os.environ, CI="true", NX_DAEMON="false", FORCE_COLOR="0", NX_SKIP_NX_CACHE="true")
    process = subprocess.Popen(command, cwd=APP, env=environment, stdout=stream,
                               stderr=subprocess.STDOUT, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except ProcessLookupError:
            pass
        raise ValueError("Suite timeout; process group stopped")


def run_service(service, sha, reports, timeout):
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=APP, text=True).strip() != sha:
        raise ValueError("Source checkout SHA mismatch")
    reports = reports.resolve()
    if APP not in reports.parents:
        raise ValueError("Reports must stay inside the app")
    directory = reports / service
    directory.mkdir(parents=True, exist_ok=True)
    report = dict(schema=1, sha=sha, service=service, status="failed", checks=[])

    def save():
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    build_failed = False
    for suite, spec in manifest()[service].items():
        result = dict(suite=suite, status="failed", report_files=[])
        if not spec["required"]:
            result.update(status="not_applicable", reason=spec["reason"], **counts())
        elif build_failed and suite != "build":
            result.update(status="blocked", reason="Build failed")
        else:
            target = directory / suite
            target.mkdir(exist_ok=True)
            log = target / "command.log"
            result["report_files"].append(log.relative_to(APP).as_posix())
            native_dir = APP / "src" / service / "target" / "surefire-reports"
            jest = APP / "reports/ci-native/checkout-integration.json"
            command = ["yarn", "nx", "run", service + ":" + spec["target"], "--skip-nx-cache", "--outputStyle=stream"]
            result["command"] = command
            try:
                if spec.get("format") == "junit":
                    for stale in native_dir.glob("TEST-*.xml"):
                        stale.unlink()
                if spec.get("format") == "jest":
                    jest.parent.mkdir(parents=True, exist_ok=True)
                    jest.unlink(missing_ok=True)
                with log.open("w", encoding="utf-8") as output:
                    result["returncode"] = execute(command, output, timeout)
                files = sorted(native_dir.glob("TEST-*.xml")) if spec.get("format") == "junit" else [jest] if spec.get("format") == "jest" else []
                for file in files:
                    dest = target / "native" / file.name
                    dest.parent.mkdir(exist_ok=True)
                    shutil.copy2(file, dest)
                    result["report_files"].append(dest.relative_to(APP).as_posix())
                if spec.get("format") == "junit":
                    result.update(parse_junit([file.read_text(encoding="utf-8") for file in files]))
                elif spec.get("format") == "jest":
                    result.update(parse_jest(json.loads(jest.read_text(encoding="utf-8"))))
                elif spec.get("format") == "go":
                    result.update(parse_go(log.read_text(encoding="utf-8", errors="replace")))
                if result["returncode"] != 0:
                    raise ValueError("Nx command failed: " + str(result["returncode"]))
                if spec.get("format"):
                    validate_counts(result)
                result["status"] = "passed"
            except (OSError, ValueError, ET.ParseError) as error:
                result["reason"] = str(error)
            if log.exists():
                print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]), flush=True)
        if suite == "build":
            build_failed = result["status"] != "passed"
        report["checks"].append(result)
        save()
    report["status"] = "passed"
    try:
        validate_source_report(report, service, sha)
    except ValueError as error:
        report.update(status="failed", reason=str(error))
    save()
    return 0 if report["status"] == "passed" else 1


def verify(plan_path, reports):
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    selected = plan["services"]
    specifications = manifest()
    if (plan.get("schema") != 1 or not isinstance(selected, list)
            or len(set(selected)) != len(selected) or not set(selected) <= set(specifications)):
        raise ValueError("Invalid source plan")
    if type(plan.get("any_source")) is not bool or plan["any_source"] != bool(selected):
        raise ValueError("Source selection flag mismatch")
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=APP, text=True).strip()
    if plan["sha"] != actual:
        raise ValueError("Plan checkout mismatch")
    for service in selected:
        report = json.loads((reports / service / "report.json").read_text(encoding="utf-8"))
        validate_source_report(report, service, actual, specifications)
        for check in report["checks"]:
            suite = check["suite"]
            expected_dir = (reports / service / suite).resolve()
            evidence_files = []
            for evidence in check.get("report_files", []):
                relative = Path(evidence)
                resolved = (APP / relative).resolve()
                if (relative.is_absolute() or ".." in relative.parts
                        or not resolved.is_relative_to(expected_dir) or not resolved.is_file()):
                    raise ValueError("Missing or unsafe source evidence: " + evidence)
                evidence_files.append(resolved)
            if len(evidence_files) != len(set(evidence_files)):
                raise ValueError("Duplicate source evidence paths")
            spec = specifications[service][suite]
            if not spec["required"] or not spec.get("format"):
                continue
            format_name = spec["format"]
            if format_name in {"junit", "jest"}:
                extension = ".xml" if format_name == "junit" else ".json"
                native = [file for file in evidence_files if file.parent == expected_dir / "native" and file.suffix == extension]
                actual_files = set((expected_dir / "native").glob("*" + extension))
                if not native or set(native) != actual_files:
                    raise ValueError("Native evidence set is incomplete")
                if format_name == "junit":
                    parsed = parse_junit([file.read_text(encoding="utf-8") for file in native])
                else:
                    if len(native) != 1:
                        raise ValueError("Expected one native Jest report")
                    parsed = parse_jest(json.loads(native[0].read_text(encoding="utf-8")))
            else:
                log = expected_dir / "command.log"
                if log not in evidence_files:
                    raise ValueError("Missing native Go event log")
                parsed = parse_go(log.read_text(encoding="utf-8", errors="replace"))
            validate_counts(parsed, spec.get("minimum_executed", 1))
            if any(check.get(key) != value for key, value in parsed.items()):
                raise ValueError("Source summary disagrees with retained native test results")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("--service", choices=tuple(manifest()), required=True)
    run.add_argument("--sha", required=True)
    run.add_argument("--reports", type=Path, default=Path("reports/source"))
    run.add_argument("--timeout", type=int, default=1200)
    gate = sub.add_parser("verify")
    gate.add_argument("--plan", type=Path, required=True)
    gate.add_argument("--reports", type=Path, default=Path("reports/source"))
    absent = sub.add_parser("unavailable")
    absent.add_argument("--service", required=True)
    absent.add_argument("--suite", required=True)
    args = parser.parse_args()
    if args.action == "run":
        return run_service(args.service, args.sha, args.reports, args.timeout)
    if args.action == "verify":
        verify(args.plan, args.reports)
        return 0
    print(manifest()[args.service][args.suite].get("reason", "Use the required suite target"), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("Source check failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)

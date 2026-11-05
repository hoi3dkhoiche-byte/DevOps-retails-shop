#!/usr/bin/env python3
"""Run native unittest cases with retained counts/JUnit; empty suites fail."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import unittest
import xml.etree.ElementTree as ET


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []
        self.current = None

    def startTest(self, test):
        super().startTest(test)
        self.current = {"id": test.id(), "started": time.monotonic(), "status": "passed", "details": []}
        self.cases.append(self.current)

    def stopTest(self, test):
        self.current["seconds"] = time.monotonic() - self.current.pop("started")
        super().stopTest(test)
        self.current = None

    def event(self, test):
        if self.current is not None:
            return self.current
        # unittest reports setUpClass/setUpModule failures without startTest.
        case = {"id": test.id(), "seconds": 0, "status": "passed", "details": [], "fixture": True}
        self.cases.append(case)
        return case

    def problem(self, status, err, test):
        # Failing subtests belong to their real parent test, not synthetic cases.
        case = self.event(test)
        case["status"] = status
        case["details"].append(self._exc_info_to_string(err, test))

    def addFailure(self, test, err):
        self.problem("failure", err, test)
        super().addFailure(test, err)

    def addError(self, test, err):
        self.problem("error", err, test)
        super().addError(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.problem("failure" if issubclass(err[0], test.failureException) else "error", err, subtest)
        super().addSubTest(test, subtest, err)

    def addSkip(self, test, reason):
        case = self.event(test)
        if self.current is not None and test.id() != self.current["id"]:
            case.setdefault("skipped_subtests", []).append(reason)
        else:
            case["status"] = "skipped"
            case["details"].append(reason)
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self.problem("expected_failure", err, test)
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        case = self.event(test)
        case["status"] = "failure"
        case["details"].append("Unexpected success of a test marked expectedFailure")
        super().addUnexpectedSuccess(test)


def xml_text(value):
    return re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "", value)


def report(result, name, directory):
    directory.mkdir(parents=True, exist_ok=True)
    executed = sum(not case.get("fixture") and case["status"] != "skipped" for case in result.cases)
    counts = {"tests": result.testsRun, "executed": executed, "skipped": len(result.skipped),
              "failures": len(result.failures), "errors": len(result.errors),
              "expected_failures": len(result.expectedFailures), "unexpected_successes": len(result.unexpectedSuccesses)}
    success = result.wasSuccessful() and executed > 0
    document = {"schema_version": 1, "suite": name, "success": success, "counts": counts,
                "source_commit": os.environ.get("GITHUB_SHA", ""), "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
                "created_at": datetime.now(timezone.utc).isoformat()}
    if not executed:
        document["gate_failure"] = "No unittest cases executed; an empty or entirely skipped suite cannot pass."
    (directory / "results.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    root = ET.Element("testsuite", name=name, tests=str(len(result.cases)),
                      failures=str(sum(case["status"] == "failure" for case in result.cases)),
                      errors=str(sum(case["status"] == "error" for case in result.cases)),
                      skipped=str(sum(case["status"] in {"skipped", "expected_failure"} for case in result.cases)))
    properties = ET.SubElement(root, "properties")
    ET.SubElement(properties, "property", name="native_tests_run", value=str(result.testsRun))
    ET.SubElement(properties, "property", name="executed_tests", value=str(executed))
    for case in result.cases:
        classname, _, method = case["id"].rpartition(".")
        item = ET.SubElement(root, "testcase", classname=classname, name=method,
                             time=f'{case.get("seconds", 0):.6f}')
        if case["status"] != "passed":
            status = "skipped" if case["status"] in {"skipped", "expected_failure"} else case["status"]
            ET.SubElement(item, status).text = xml_text("\n".join(case["details"]))
        if case.get("skipped_subtests"):
            ET.SubElement(item, "system-out").text = xml_text("Skipped subtests: " + "; ".join(case["skipped_subtests"]))
    ET.ElementTree(root).write(directory / "junit.xml", encoding="utf-8", xml_declaration=True)
    line = (f'{name}: {counts["tests"]} discovered, {executed} executed, {counts["skipped"]} skipped, '
            f'{counts["failures"]} failures, {counts["errors"]} errors; {"PASS" if success else "FAIL"}.')
    print(line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as target:
            target.write("\n" + line + "\n")
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-directory", type=Path, required=True)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--name", required=True)
    parser.add_argument("--report-directory", type=Path, required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(args.start_directory), pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
    return report(result, args.name, args.report_directory)


if __name__ == "__main__":
    raise SystemExit(main())

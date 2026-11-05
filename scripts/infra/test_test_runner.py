"""Exercise the native unittest evidence gate, including misleading empty suites."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import run_tests


class NativeEvidenceTests(unittest.TestCase):
    def execute(self, cases):
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=run_tests.EvidenceResult).run(
            unittest.TestSuite(cases))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch("builtins.print"), patch.dict(run_tests.os.environ, {"GITHUB_STEP_SUMMARY": ""}):
                code = run_tests.report(result, "fixture", directory)
            return code, json.loads((directory / "results.json").read_text()), ET.parse(directory / "junit.xml")

    def test_empty_and_entirely_skipped_suites_fail_with_real_counts(self):
        class Example(unittest.TestCase):
            @unittest.skip("Fixture skip")
            def test_skip(self):
                pass
        for cases, expected in (([], 0), ([Example("test_skip")], 1)):
            with self.subTest(cases=expected):
                code, evidence, document = self.execute(cases)
                self.assertEqual(code, 1)
                self.assertEqual(evidence["counts"]["tests"], expected)
                self.assertEqual(evidence["counts"]["executed"], 0)
                self.assertIn("gate_failure", evidence)
                self.assertEqual(len(document.findall("testcase")), expected)

    def test_passing_suite_keeps_skips_explicit_and_escapes_xml(self):
        class Example(unittest.TestCase):
            def test_pass(self):
                pass
            @unittest.skip("Fixture <skip> & explanation")
            def test_skip(self):
                pass
        code, evidence, document = self.execute([Example("test_pass"), Example("test_skip")])
        self.assertEqual(code, 0)
        self.assertEqual((evidence["counts"]["executed"], evidence["counts"]["skipped"]), (1, 1))
        self.assertIn("<skip> &", document.find("testcase/skipped").text)

    def test_failure_error_and_failing_subtest_are_retained(self):
        class Example(unittest.TestCase):
            def test_failure(self):
                self.fail("Fixture assertion")
            def test_error(self):
                raise ValueError("Fixture error")
            def test_subtest(self):
                with self.subTest(item=1):
                    self.fail("Fixture subtest")
        code, evidence, document = self.execute([Example("test_failure"), Example("test_error"), Example("test_subtest")])
        self.assertEqual(code, 1)
        self.assertEqual((evidence["counts"]["failures"], evidence["counts"]["errors"]), (2, 1))
        self.assertEqual(len(document.findall("testcase/failure")), 2)
        self.assertEqual(len(document.findall("testcase/error")), 1)
        self.assertEqual(document.getroot().attrib["tests"], "3")

    def test_class_setup_failure_retains_error_without_claiming_executed_tests(self):
        class Example(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("Fixture setup failed")
            def test_never_runs(self):
                pass
        code, evidence, document = self.execute([Example("test_never_runs")])
        self.assertEqual(code, 1)
        self.assertEqual((evidence["counts"]["tests"], evidence["counts"]["executed"], evidence["counts"]["errors"]), (0, 0, 1))
        self.assertIn("Fixture setup failed", document.find("testcase/error").text)

    def test_skipped_subtest_does_not_hide_the_parent_test_execution(self):
        class Example(unittest.TestCase):
            def test_subcases(self):
                with self.subTest(item=1):
                    self.skipTest("Fixture optional subcase")
                with self.subTest(item=2):
                    self.assertTrue(True)
        code, evidence, document = self.execute([Example("test_subcases")])
        self.assertEqual(code, 0)
        self.assertEqual(evidence["counts"]["executed"], 1)
        self.assertIn("Fixture optional subcase", document.find("testcase/system-out").text)


if __name__ == "__main__":
    unittest.main()

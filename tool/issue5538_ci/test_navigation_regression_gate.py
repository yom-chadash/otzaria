import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("navigation_gate", Path(__file__).with_name("navigation_regression_gate.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class TestNavigationGate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.report = Path(self.temp.name) / "report.jsonl"

    def baseline(self):
        rows = []
        for index, name in enumerate(sorted(gate.CORE_NAMES)):
            rows.extend([
                {"type": "testStart", "test": {"id": index, "name": name}},
                {"type": "error", "testID": index, "isFailure": True, "error": gate.EXPECTED_REASON},
                {"type": "testDone", "testID": index, "result": "failure", "hidden": False, "skipped": False},
            ])
        rows.append({"type": "done", "success": False})
        return rows

    def write(self, rows):
        self.report.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    def test_accepts_only_the_four_expected_assertion_failures(self):
        self.write(self.baseline())
        self.assertEqual(gate.validate_report(self.report, 1, True)["visibleTests"], 4)

    def test_rejects_compile_failure_instead_of_regression(self):
        rows = self.baseline()
        rows[1].update(isFailure=False, error="Compilation failed")
        self.write(rows)
        with self.assertRaises(ValueError): gate.validate_report(self.report, 1, True)

    def test_rejects_different_assertion(self):
        rows = self.baseline()
        rows[1]["error"] = "Could not load the document"
        self.write(rows)
        with self.assertRaises(ValueError): gate.validate_report(self.report, 1, True)

    def test_rejects_incomplete_stream(self):
        self.write(self.baseline()[:-1])
        with self.assertRaises(ValueError): gate.validate_report(self.report, 1, True)

    def test_rejects_skipped_case(self):
        rows = self.baseline()
        rows[2]["skipped"] = True
        self.write(rows)
        with self.assertRaises(ValueError): gate.validate_report(self.report, 1, True)

    def test_rejects_unexpected_setup_error(self):
        rows = self.baseline()
        rows.insert(-1, {"type": "error", "testID": 999, "isFailure": False, "error": "native asset missing"})
        self.write(rows)
        with self.assertRaises(ValueError): gate.validate_report(self.report, 1, True)

    def test_candidate_requires_all_eleven_navigation_cases(self):
        names = sorted(gate.CORE_NAMES) + [f"navigation owner case {i}" for i in range(7)]
        rows = []
        for index, name in enumerate(names):
            rows.extend([
                {"type": "testStart", "test": {"id": index, "name": name}},
                {"type": "testDone", "testID": index, "result": "success", "hidden": False, "skipped": False},
            ])
        rows.append({"type": "done", "success": True})
        self.write(rows)
        self.assertEqual(gate.validate_report(self.report, 0, False)["visibleTests"], 11)
        for index in (20, 21):
            rows[-1:-1] = [
                {"type": "testStart", "test": {"id": index, "name": "existing repeated suite name"}},
                {"type": "testDone", "testID": index, "result": "success", "hidden": False, "skipped": False},
            ]
        self.write(rows)
        self.assertEqual(gate.validate_report(self.report, 0, False)["visibleTests"], 13)
        self.write(rows[2:])
        with self.assertRaises(ValueError): gate.validate_report(self.report, 0, False)


if __name__ == "__main__":
    unittest.main()

"""Corruption checks for the fork-only E03 evidence contract."""

import copy
from pathlib import Path
import tempfile
import unittest

import internal_text_run as producer


class EvidenceContract(unittest.TestCase):
    def setUp(self):
        self.config = {"sourceCommit": "a" * 40, "inputSha256": "b" * 64,
                       "pdfiumSha256": "c" * 64, "pageCount": 2,
                       "warmup": 1, "repetitions": 1, "referencePage": 2}
        proof = {"hasRawText": True, "utf16CodeUnits": 1100, "characterRects": 1100,
                 "textSha256": "d" * 64, "geometrySha256": "e" * 64}
        self.report = {"measurementComplete": True, "executionMode": "dart-aot", "instrumented": True,
                       **{key: value for key, value in self.config.items() if key != "referencePage"},
                       "observations": [],
                       "reference": {"pageNumber": 2, "timed": False, "normalized": proof.copy(),
                                     "beforeHebrewTransforms": {**proof, "hebrewMarks": 10}}}
        for iteration in range(2):
            for page in (1, 2):
                self.report["observations"].append({
                    "iteration": iteration, "pageNumber": page, "warmup": iteration == 0,
                    "callerRawLoadUs": 150, "outsideMeasuredWorkerBodyUs": 30, **proof,
                    "worker": {"pageNumber": page, "completed": True, "normalizeHebrew": True,
                               "workerRawBuildUs": 70, "workerNormalizationUs": 30,
                               "workerCleanupUs": 10, "workerBodyUs": 120},
                })

    def check_report(self, value=None, instrumented=True):
        return producer.validate_report(value or self.report, self.config, instrumented)

    def test_complete_control_and_instrumented(self):
        self.assertEqual(len(self.check_report()), 2)
        control = copy.deepcopy(self.report)
        control["instrumented"] = False
        for row in control["observations"]:
            row.pop("worker")
            row.pop("outsideMeasuredWorkerBodyUs")
        self.assertEqual(self.check_report(control, False), self.check_report())

    def test_missing_and_duplicate_pages_rejected(self):
        missing = copy.deepcopy(self.report)
        missing["observations"].pop()
        with self.assertRaises(ValueError):
            self.check_report(missing)
        duplicate = copy.deepcopy(self.report)
        duplicate["observations"][3] = duplicate["observations"][2]
        with self.assertRaises(ValueError):
            self.check_report(duplicate)

    def test_worker_and_residual_corruption_rejected(self):
        for field, value in (("workerBodyUs", 200), ("workerNormalizationUs", 80), ("workerCleanupUs", -1)):
            changed = copy.deepcopy(self.report)
            changed["observations"][0]["worker"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check_report(changed)
        self.report["observations"][0]["outsideMeasuredWorkerBodyUs"] = 0
        with self.assertRaises(ValueError):
            self.check_report()

    def test_full_geometry_and_text_parity_required(self):
        for field in ("textSha256", "geometrySha256"):
            changed = copy.deepcopy(self.report)
            changed["observations"][-1][field] = "f" * 64
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check_report(changed)

    def test_blank_reference_and_identity_drift_rejected(self):
        changed = copy.deepcopy(self.report)
        changed["reference"]["beforeHebrewTransforms"]["utf16CodeUnits"] = 0
        with self.assertRaises(ValueError):
            self.check_report(changed)
        self.report["sourceCommit"] = "f" * 40
        with self.assertRaises(ValueError):
            self.check_report()

    def test_instrumented_control_rejected(self):
        self.report["instrumented"] = False
        with self.assertRaises(ValueError):
            self.check_report(instrumented=False)

    def test_summary_keeps_processes_and_passes_distinct(self):
        control = copy.deepcopy(self.report)
        control["instrumented"] = False
        result = producer.timing_summary([control, self.report, self.report, control])
        self.assertEqual(result["instrumented"]["independentProcesses"], 2)
        self.assertEqual(result["instrumented"]["processes"][0]["passMedianUs"]["workerUnattributedUs"], 20)

    def test_reference_query_does_not_remove_spaces(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "text.txt"
            path.write_text("אֱלֹהִים אֱ לֹהִים אלהים", encoding="utf-8")
            result = producer.text_summary(path)
        self.assertEqual(result["literalHits"], 1)
        self.assertEqual(result["niqqudInsensitiveHits"], 2)

    def test_reference_preserves_crlf_and_counts_utf16(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "text.txt"
            path.write_bytes("א\r\n😀".encode("utf-8"))
            result = producer.text_summary(path)
        self.assertEqual(result["utf16CodeUnits"], 5)
        self.assertEqual(result["utf8Bytes"], 8)


if __name__ == "__main__":
    unittest.main()

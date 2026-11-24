import copy
import json
from pathlib import Path
import tempfile
import unittest

import run_pdf_highlight_pair as pair
import validate_pdf_highlight_profile as profile


def fixture(*, count=17, elapsed=100, microtasks=20, commit="a" * 40):
    queries = [{"id": query} for query in profile.QUERIES]
    rows = [
        {"iteration": iteration, "warmup": iteration == 0, "queryId": query,
         "pageNumber": page, "elapsedUs": elapsed, "matches": 2, "rects": 2,
         "resultsSha256": "1" * 64, "parity": True}
        for iteration in range(2) for query in profile.QUERIES for page in range(1, count + 1)
    ]
    inputs = [("real-document-metadata", count)] + [
        (kind, length) for kind in ("derived-real-metadata-repeated", "synthetic-mixed-sizes") for length in (100, 1000, 10000)
    ]
    return {
        "protocolVersion": 1, "status": "passed", "executionMode": "flutter-profile",
        "component": "otzaria-page-highlights-and-book-layout", "pdfiumSha256": profile.PDFIUM_SHA256,
        "dartVersion": "3.13.5 (test fixture)", "sourceCommit": commit, "highlightSourceSha256": "2" * 64,
        "inputSha256": "3" * 64, "inputBytes": 1000, "uiLatencyCollected": False, "microtaskAuditTimed": False,
        "contextLength": 80, "maxCachedPages": 8, "normalizeHebrewText": True, "decodeLegacyHebrewText": True,
        "highlightScope": "actual app ensurePage with preloaded real structured text; extraction excluded",
        "sourcePageCount": count, "warmup": 1, "repetitions": 1, "queries": queries,
        "textInventory": [{"pageNumber": page, "textSha256": "4" * 64, "textCharacters": 10, "characterRects": 10, "fragments": 1} for page in range(1, count + 1)],
        "referenceMatchesPerQuery": {query: count * 2 for query in profile.QUERIES},
        "host": {"name": "test-fixture", "processors": 2}, "observations": rows,
        "microtaskAudit": [{"queryId": query, "pageNumber": page, "scheduledMicrotasks": microtasks, "matches": 2, "resultsSha256": "1" * 64, "parity": True} for query in profile.QUERIES for page in range(1, count + 1)],
        "layout": [
            {"inputKind": kind, "pageCount": length, "iteration": iteration, "warmup": iteration == 0,
             "scenario": scenario, "sourceDocumentPageCount": None if kind == "synthetic-mixed-sizes" else count,
             "sameLayoutInstance": scenario == "cache_hit", "elapsedUs": 10, "widthReads": length,
             "heightReads": length, "layoutSha256": "5" * 64, "parity": True}
            for kind, length in inputs for iteration in range(2) for scenario in profile.SCENARIOS
        ],
    }


class ProfileValidationTest(unittest.TestCase):
    def write(self, directory, value):
        directory.mkdir(parents=True)
        path = directory / "report.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        (directory / "observations.jsonl").write_text("".join(json.dumps(row) + "\n" for row in value["observations"]), encoding="utf-8")
        return path

    def test_complete_passes_exclude_warmup_and_keep_untimed_counts_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = profile.validate_report(self.write(root / "before", fixture()))
            after = profile.validate_report(self.write(root / "after", fixture(elapsed=50, microtasks=3, commit="b" * 40)))
            result = profile.compare([before], [after])
            change = result["highlightChanges"][profile.QUERIES[0]]
            self.assertEqual(change["p50BeforeUs"], 1700)
            self.assertEqual(change["p50AfterUs"], 850)
            self.assertEqual(change["p50ChangePercent"], -50)
            self.assertEqual(change["microtasksBefore"], 340)
            self.assertEqual(change["microtasksAfter"], 51)
            self.assertEqual(result["baseline"]["highlight"][profile.QUERIES[0]]["completePassUs"]["count"], 1)

    def test_missing_result_or_forged_warmup_is_rejected(self):
        for mutate in (
            lambda value: value["observations"].pop(),
            lambda value: value["observations"][0].update(warmup=False),
            lambda value: value["microtaskAudit"].pop(),
        ):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temporary:
                value = fixture()
                mutate(value)
                with self.assertRaises(ValueError):
                    profile.validate_report(self.write(Path(temporary) / "run", value))

    def test_small_documents_are_measured_without_claiming_cache_eviction(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = profile.validate_report(self.write(Path(temporary) / "run", fixture(count=2)))
            summary = profile.summarize([report])
            self.assertEqual(summary["highlight"][profile.QUERIES[0]]["matchesPerPass"], 4)

    def test_geometry_or_hardware_drift_between_runs_is_rejected(self):
        before = fixture()
        for field, changed in (("resultsSha256", "6" * 64),):
            after = copy.deepcopy(before)
            for row in after["observations"]:
                row[field] = changed
            with self.assertRaises(ValueError):
                profile.compare([before], [after])
        after = copy.deepcopy(before)
        after["host"]["name"] = "different-runner"
        with self.assertRaises(ValueError):
            profile.compare([before], [after])

    def test_repeated_metadata_cannot_be_reported_as_real_document_pages(self):
        value = fixture()
        next(row for row in value["layout"] if row["inputKind"] == "derived-real-metadata-repeated")["sourceDocumentPageCount"] = 10000
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                profile.validate_report(self.write(Path(temporary) / "run", value))

    def test_pair_rejects_unrelated_product_or_harness_changes(self):
        before = {"files": {name: "1" * 64 for name in (*pair.SHARED, pair.HIGHLIGHTS)}}
        after = copy.deepcopy(before)
        after["files"][pair.HIGHLIGHTS] = "2" * 64
        pair.validate_source_pair(before, after, [pair.HIGHLIGHTS])
        with self.assertRaises(ValueError):
            pair.validate_source_pair(before, after, [pair.HIGHLIGHTS, "lib/main.dart"])
        after["files"][pair.TARGET] = "3" * 64
        with self.assertRaises(ValueError):
            pair.validate_source_pair(before, after, [pair.HIGHLIGHTS])

    def test_plan_requires_immutable_different_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            for value in ({"baseline": "dev", "candidate": "b" * 40},
                          {"baseline": "a" * 40, "candidate": "a" * 40}):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    pair.load_plan(path)

    def test_corpus_mutation_is_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "small.pdf"
            pdf.write_bytes(b"manifest identity fixture")
            manifest = root / "corpus.json"
            manifest.write_text(json.dumps({"documents": [{"id": "small", "path": "small.pdf", "sha256": pair.digest(pdf), "sizeBytes": pdf.stat().st_size, "pageCount": 2}]}), encoding="utf-8")
            self.assertEqual(pair.load_documents(manifest, ["small"])[0]["pageCount"], 2)
            pdf.write_bytes(b"changed corpus")
            with self.assertRaises(ValueError):
                pair.load_documents(manifest, ["small"])


if __name__ == "__main__":
    unittest.main()

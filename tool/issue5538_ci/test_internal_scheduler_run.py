import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import internal_scheduler_run as runner
import internal_scheduler_scan as scan


def sample(mode="normal", requests=100, version="before", warmup=False):
    cooperative = requests if mode == "cooperative" else ((requests + 99) // 100 if mode == "mixed" else 0)
    fixture = {"mode": mode, "requests": requests, "cooperativeRequests": cooperative, "steps": 3}
    return {"fixture": fixture, "fixtureSha256": hashlib.sha256(
                json.dumps(fixture, separators=(",", ":")).encode()).hexdigest(),
            "cooperativeSteps": cooperative * 3, "cooperativeDisposals": cooperative,
            "dart": "3.13.5 (stable) on linux_x64", "os": "linux", "wallMicros": 20,
            "processCpuMicros": 10, "peakRssBytes": 1000, "version": version, "warmup": warmup}


def count(mode="normal", requests=100):
    cooperative = requests if mode == "cooperative" else ((requests + 99) // 100 if mode == "mixed" else 0)
    return {"mode": mode, "requests": requests, "steps": 3, "cooperativeRequests": cooperative,
            "ordinaryCompletions": requests - cooperative, "cooperativeSteps": cooperative * 3,
            "cooperativeDisposals": cooperative, "cooperativeCancellationReads": cooperative * 12,
            "ordinaryCancellationReads": requests * (requests + 1) // 2 if mode == "normal" else 10}


class SchedulerContracts(unittest.TestCase):
    def test_only_worker_runtime_delta_and_new_regression_are_allowed(self):
        before = {runner.WORKER: "old", "pubspec.yaml": "same"}
        after = {**before, runner.WORKER: "new", runner.REGRESSION: "new test"}
        self.assertEqual(runner.verify_delta(before, after), sorted((runner.WORKER, runner.REGRESSION)))
        after["lib/other.dart"] = "different"
        with self.assertRaisesRegex(ValueError, "more than"):
            runner.verify_delta(before, after)

    def test_existing_regression_is_not_mistaken_for_the_frozen_baseline(self):
        before = {runner.WORKER: "old", runner.REGRESSION: "old test"}
        after = {runner.WORKER: "new", runner.REGRESSION: "new test"}
        with self.assertRaisesRegex(ValueError, "new regression"):
            runner.verify_delta(before, after)

    def test_normal_counter_preserves_work_and_removes_quadratic_reads(self):
        before = count(requests=4096)
        after = {**before, "ordinaryCancellationReads": 0}
        runner.validate_scan_pair(before, after, "normal", 4096)
        before["ordinaryCancellationReads"] -= 1
        with self.assertRaisesRegex(ValueError, "prequeued"):
            runner.validate_scan_pair(before, after, "normal", 4096)

    def test_mixed_cooperative_polling_must_be_identical(self):
        before = count("mixed", 10000)
        after = {**before, "ordinaryCancellationReads": 0}
        runner.validate_scan_pair(before, after, "mixed", 10000)
        after["cooperativeCancellationReads"] += 1
        with self.assertRaisesRegex(ValueError, "cooperative behavior"):
            runner.validate_scan_pair(before, after, "mixed", 10000)

    def test_matching_but_incomplete_work_is_rejected(self):
        before = count("mixed", 4096)
        before["cooperativeDisposals"] -= 1
        after = {**before, "ordinaryCancellationReads": 0}
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            runner.validate_scan_pair(before, after, "mixed", 4096)

    def test_sample_rejects_sdk_or_fixture_drift(self):
        value = sample("mixed", 4096)
        runner.validate_sample(value, "mixed", 4096)
        value["dart"] = "3.14.0 (stable)"
        with self.assertRaisesRegex(ValueError, "different SDK"):
            runner.validate_sample(value, "mixed", 4096)
        value = sample()
        value["fixtureSha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "fixture"):
            runner.validate_sample(value, "normal", 100)

    def test_summary_excludes_only_explicit_warmups_and_keeps_outliers(self):
        samples = []
        for mode, requests in runner.FIXTURES:
            for version in runner.SOURCES:
                samples += [sample(mode, requests, version) for _ in range(5)]
                samples[-1]["wallMicros"] = 1000
                warmup = sample(mode, requests, version, warmup=True)
                warmup["wallMicros"] = 100000
                samples.append(warmup)
        for row in runner.summary(samples):
            self.assertEqual(row["variants"]["after"]["wallMicros"], {"median": 20, "min": 20, "max": 1000})
        with self.assertRaisesRegex(ValueError, "five retained"):
            runner.summary(samples[1:])

    def test_duplicate_or_missing_observation_is_rejected(self):
        value = json.dumps({"fixture": {}})
        self.assertEqual(runner.parse_observation("build diagnostic\n" + value, "fixture"), {"fixture": {}})
        for text in ("build diagnostic", value + "\n" + value):
            with self.assertRaisesRegex(ValueError, "one complete"):
                runner.parse_observation(text, "fixture")

    def test_counter_instrumentation_rejects_an_unknown_getter(self):
        source = "import '../pdfrx.dart';\nclass _ComputeParams {\nbool get isCanceled => false;\n}\nclass _ExecuteParams {}"
        result = scan.instrument_source(source)
        self.assertIn("_ordinaryCancellationReads++; return false;", result)
        self.assertIn("import 'package:pdfrx_engine/src/pdfrx.dart';", result)
        with self.assertRaisesRegex(ValueError, "getter changed"):
            scan.instrument_source(source.replace("=> false", "=> true"))

    def test_scan_receipts_cannot_overwrite_different_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt"
            scan.retain(path, b"original")
            scan.retain(path, b"original")
            with self.assertRaisesRegex(ValueError, "differs"):
                scan.retain(path, b"changed")
            self.assertEqual(path.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()

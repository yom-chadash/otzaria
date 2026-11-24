"""Evidence-integrity contracts; no desktop or Dart compiler is launched."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from pypdf import PdfReader

import font_fallback_fixtures as fixtures
import run_font_fallback_profile as runner


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        path = self.root / "frame.png"
        image = Image.new("RGBA", (320, 400), (255, 255, 255, 255))
        image.save(path)
        frame = {"phase": "measurement", "inputId": "unrelated", "documentId": 0,
                 "pageNumber": 1, "renderState": "ready", "elapsedUs": 50, "stableDocument": True,
                 "width": 320, "height": 400, "rgbaSha256": hashlib.sha256(image.tobytes()).hexdigest(),
                 "pngSha256": runner.digest(path), "pngPath": "frame.png"}
        reference = {**frame, "phase": "reference"}
        runner.save(self.root / "frame-receipts.json", {"measured": [frame], "reference": [reference]})
        proof = {"text": "", "textSha256": hashlib.sha256(b"").hexdigest(), "utf16CodeUnits": 0,
                 "geometrySha256": "b" * 64, "rasterSha256": "c" * 64, "rasterWidth": 320,
                 "rasterHeight": 400, "inkPixels": 0, "stableDocument": True}
        parity = {"textEqual": True, "geometryEqual": True, "fullPageRasterEqual": True,
                  "stableMeasuredDocument": True, "referenceContainsExpectedWord": True,
                  "referenceHasInk": True, "firstGoodSampleUs": 50, "samples": 1,
                  "sampledReadyFramesBeforeRecovery": 0, "sampledMismatchingReadyFrames": 0,
                  "sampledPendingFrames": 0}
        self.config = {"sourceCommit": "a" * 40, "appSourcesSha256": "d" * 64, "variant": "C",
                       "scenario": "close-pending-font", "route": "app", "documents": [
                           {"id": "source-0", "absolutePath": str(self.root / "source.pdf"), "pageCount": 1,
                            "requestedFace": "MissingFace", "targetPage": 1, "expectedWord": "שלום"},
                           {"id": "unrelated", "absolutePath": str(self.root / "blank.pdf"), "pageCount": 1,
                            "requestedFace": None, "targetPage": 1, "expectedWord": ""}]}
        events = [
            {"type": "nativeOpen", "documentId": 0, "path": str(self.root / "blank.pdf"), "pageCount": 1},
            {"type": "nativeOpen", "documentId": 1, "path": str(self.root / "source.pdf"), "pageCount": 1},
            {"type": "missingFonts", "documentId": 1, "eventOwnsDocument": True,
             "queries": [{"face": "MissingFace"}], "byPage": {"1": []}},
        ]
        self.report = {"status": "completed", "mode": "profile", "pid": 123,
                       **{key: self.config[key] for key in ("sourceCommit", "appSourcesSha256", "variant", "scenario", "route")},
                       "allNativeCloseFuturesCompleted": True, "frameworkErrors": [], "frameCaptureErrors": [],
                       "measuredFrames": [frame], "referenceFrames": {"unrelated": frame["rgbaSha256"]},
                       "measuredEvents": events, "allEvents": events + [
                           {"type": "documentEventsClosed", "documentId": 0},
                           {"type": "documentEventsClosed", "documentId": 1}],
                       "finalProofs": {"unrelated": proof}, "referenceProofs": {"unrelated": dict(proof)},
                       "parity": {"unrelated": parity}}

    def validate(self):
        return runner.validate_result(self.root, self.report, self.config, 123)

    def test_complete_owned_result(self):
        self.assertEqual(self.validate()["nativeOpenCounts"], {"source-0": 1, "unrelated": 1})

    def test_recomputed_png_rejects_wrong_raw_rgba_hash(self):
        row = self.report["measuredFrames"][0]
        row["rgbaSha256"] = "0" * 64
        sidecar = json.loads((self.root / "frame-receipts.json").read_text())
        sidecar["measured"] = self.report["measuredFrames"]
        runner.save(self.root / "frame-receipts.json", sidecar)
        with self.assertRaisesRegex(ValueError, "Decoded native frame"):
            self.validate()

    def test_first_good_must_come_from_an_actual_frame(self):
        self.report["parity"]["unrelated"]["firstGoodSampleUs"] = 1
        with self.assertRaisesRegex(ValueError, "First-good"):
            self.validate()

    def test_matching_placeholder_is_not_a_first_good_frame(self):
        self.report["measuredFrames"][0]["renderState"] = "pending"
        sidecar = json.loads((self.root / "frame-receipts.json").read_text())
        sidecar["measured"] = self.report["measuredFrames"]
        runner.save(self.root / "frame-receipts.json", sidecar)
        with self.assertRaisesRegex(ValueError, "First-good"):
            self.validate()

    def test_parity_is_recomputed(self):
        self.report["finalProofs"]["unrelated"]["rasterSha256"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "reported parity"):
            self.validate()

    def test_closed_document_cannot_be_reopened(self):
        self.report["measuredEvents"].append({"type": "nativeOpen", "documentId": 2,
            "path": str(self.root / "source.pdf"), "pageCount": 1})
        self.report["allEvents"].append({"type": "documentEventsClosed", "documentId": 2})
        with self.assertRaisesRegex(ValueError, "Closed/replaced"):
            self.validate()

    def test_other_document_cannot_receive_font_query(self):
        self.report["measuredEvents"][2]["documentId"] = 0
        with self.assertRaisesRegex(ValueError, "wrong-document"):
            self.validate()

    def test_missing_cleanup_receipt_fails(self):
        self.report["allEvents"] = self.report["allEvents"][:-1]
        with self.assertRaisesRegex(ValueError, "event stream"):
            self.validate()

    def test_missing_scope_does_not_silently_pass(self):
        self.report["parity"] = {}
        with self.assertRaisesRegex(ValueError, "Incomplete actual"):
            self.validate()

    def test_frame_path_cannot_escape_evidence(self):
        sidecar = json.loads((self.root / "frame-receipts.json").read_text())
        for row in sidecar["measured"] + sidecar["reference"]:
            row["pngPath"] = str(self.root / "frame.png")
        self.report["measuredFrames"] = sidecar["measured"]
        runner.save(self.root / "frame-receipts.json", sidecar)
        with self.assertRaisesRegex(ValueError, "Frame path escapes"):
            self.validate()

    def test_bundle_receipt_includes_aot_payload_drift(self):
        bundle = self.root / "bundle"
        (bundle / "lib").mkdir(parents=True)
        for name in ("otzaria", "lib/libapp.so", "lib/libflutter_linux_gtk.so"):
            (bundle / name).write_bytes(name.encode())
        before = runner.bundle_receipt(bundle)
        (bundle / "lib/libapp.so").write_bytes(b"changed app code")
        self.assertNotEqual(before, runner.bundle_receipt(bundle))

    def test_orchestrator_must_match_every_frozen_harness_file(self):
        files = {name: runner.digest(runner.ROOT / name) for name in runner.HARNESS}
        orchestrator = {"files": files}
        receipts = {name: {"files": dict(files)} for name in ("A", "B", "C")}
        runner.validate_orchestrator(orchestrator, receipts)
        receipts["C"]["files"]["tool/perf_probe/run_perf_probe_posix.py"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Orchestrator harness differs"):
            runner.validate_orchestrator(orchestrator, receipts)

    def _framework_builds(self, changed=None):
        builds = {}
        for name in ("A", "B", "C"):
            bundle = self.root / name
            (bundle / "lib").mkdir(parents=True)
            executable = bundle / "otzaria"
            executable.write_bytes(b"application")
            (bundle / "lib/libflutter_linux_gtk.so").write_bytes(
                b"different framework" if name == changed else b"same framework")
            builds[name] = {"engine": runner.BundledFlutterEngine(executable, "linux", "profile")}
        return builds

    def test_common_framework_uses_actual_producer_schema(self):
        identities = runner.validate_common_framework(self._framework_builds())
        self.assertEqual(set(identities), {"A", "B", "C"})
        expected = hashlib.sha256(b"same framework").hexdigest()
        self.assertTrue(all(identity["bundleSha256"] == expected for identity in identities.values()))

    def test_different_framework_is_rejected_before_launch(self):
        with self.assertRaisesRegex(ValueError, "Framework binary differs"):
            runner.validate_common_framework(self._framework_builds(changed="C"))

    def test_missing_framework_hash_cannot_bypass_comparison(self):
        builds = self._framework_builds()
        del builds["B"]["engine"].identity["bundleSha256"]
        with self.assertRaisesRegex(ValueError, "missing its bundled binary SHA256"):
            runner.validate_common_framework(builds)

    def test_fixtures_are_unembedded_and_repeatable(self):
        first = fixtures.prepare(self.root / "first")
        second = fixtures.prepare(self.root / "second")
        self.assertEqual(first, second)
        for case in first["cases"]:
            for item in case["documents"]:
                path = self.root / "first" / item["path"]
                self.assertEqual(runner.digest(path), item["sha256"])
                reader = PdfReader(path)
                self.assertEqual(len(reader.pages), item["pageCount"])
                for page in reader.pages:
                    for font in page["/Resources"].get("/Font", {}).values():
                        descriptor = font.get_object()["/FontDescriptor"]
                        self.assertFalse({"/FontFile", "/FontFile2", "/FontFile3"} & set(descriptor))


if __name__ == "__main__":
    unittest.main()

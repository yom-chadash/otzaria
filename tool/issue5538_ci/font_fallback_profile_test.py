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
                 "captureStartedUs": 40, "renderStateAfterCapture": "ready", "stableView": True,
                 "width": 320, "height": 400, "rgbaSha256": hashlib.sha256(image.tobytes()).hexdigest(),
                 "pngSha256": runner.digest(path), "pngPath": "frame.png"}
        reference = {**frame, "phase": "reference"}
        runner.save(self.root / "frame-receipts.json", {"measured": [frame], "reference": [reference]})
        proof = {"text": "", "textSha256": hashlib.sha256(b"").hexdigest(), "utf16CodeUnits": 0,
                 "geometrySha256": "b" * 64, "rasterSha256": "c" * 64, "rasterWidth": 320,
                 "rasterHeight": 400, "inkPixels": 0, "stableDocument": True}
        parity = {"textEqual": True, "geometryEqual": True, "fullPageRasterEqual": True,
                  "stableMeasuredDocument": True, "referenceContainsExpectedWord": True,
                  "referenceContainsControlText": True,
                  "referenceHasInk": True, "firstGoodSampleUs": 50, "samples": 1,
                  "sampledReadyFramesBeforeRecovery": 0, "sampledMismatchingReadyFrames": 0,
                  "sampledPendingFrames": 0}
        self.config = {"sourceCommit": "a" * 40, "appSourcesSha256": "d" * 64, "variant": "C",
                       "scenario": "close-pending-font", "route": "app", "documents": [
                           {"id": "source-0", "absolutePath": str(self.root / "source.pdf"), "pageCount": 1,
                            "requestedFace": "MissingFace", "targetPage": 1, "expectedWord": "שלום",
                            "fontResourcesByPage": {"1": ["MissingFace", "Helvetica"]},
                            "expectedControlText": "Standard14 control"},
                           {"id": "unrelated", "absolutePath": str(self.root / "blank.pdf"), "pageCount": 1,
                            "requestedFace": None, "targetPage": 1, "expectedWord": "",
                            "fontResourcesByPage": {}, "expectedControlText": ""}]}
        (self.root / "source.pdf").write_bytes(fixtures.pdf("MissingFace"))
        (self.root / "blank.pdf").write_bytes(fixtures.pdf(None))
        events = [
            {"type": "nativeOpen", "documentId": 0, "path": str(self.root / "blank.pdf"), "pageCount": 1},
            {"type": "nativeOpen", "documentId": 1, "path": str(self.root / "source.pdf"), "pageCount": 1},
            {"type": "missingFonts", "documentId": 1, "eventOwnsDocument": True,
             "queries": [{"face": "MissingFace", "weight": 400, "italic": False,
                          "charset": 0, "pitchFamily": 0}], "byPage": {"1": []}},
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

    def replacement(self):
        self.config['scenario'] = self.report['scenario'] = 'replace-pending-font'
        events = self.report['measuredEvents']
        events[0].update(startedUs=1, elapsedUs=5)
        events[1].update(startedUs=2, elapsedUs=6)
        events.extend([
            {'type': 'fontRegistrationStart', 'elapsedUs': 10, 'face': 'MissingFace'},
            {'type': 'documentEventsClosed', 'elapsedUs': 18, 'documentId': 0},
            {'type': 'replaceRequested', 'elapsedUs': 20, 'inputId': 'unrelated'},
            {'type': 'documentEventsClosed', 'elapsedUs': 21, 'documentId': 1},
            {'type': 'nativeOpen', 'documentId': 2, 'path': str(self.root / 'blank.pdf'),
             'pageCount': 1, 'startedUs': 22, 'elapsedUs': 25},
            {'type': 'viewerDocument', 'elapsedUs': 26, 'inputId': 'unrelated', 'documentId': 2},
            {'type': 'registrationGateReleased', 'elapsedUs': 40},
            {'type': 'fontRegistered', 'elapsedUs': 45, 'face': 'MissingFace'},
        ])
        self.report['allEvents'] = events + [{'type': 'documentEventsClosed', 'elapsedUs': 80, 'documentId': 2}]
        self.report['measuredFrames'][0]['documentId'] = 2
        sidecar = json.loads((self.root / 'frame-receipts.json').read_text())
        sidecar['measured'] = self.report['measuredFrames']
        runner.save(self.root / 'frame-receipts.json', sidecar)

    def test_replacement_counts_both_intentional_opens_before_font_completion(self):
        self.replacement()
        result = self.validate()
        self.assertEqual(result['nativeOpenCounts'], {'source-0': 1, 'unrelated': 2})
        self.assertEqual(result['replacementLifecycle']['nativeOpensAfterRegistrationRelease'], 0)
        self.assertEqual(result['replacementLifecycle']['replacementDocumentId'], 2)

    def test_replacement_requires_one_actual_replace_request(self):
        self.replacement()
        request = next(row for row in self.report['measuredEvents'] if row['type'] == 'replaceRequested')
        self.report['measuredEvents'].remove(request)
        with self.assertRaisesRegex(ValueError, 'one explicit request'):
            self.validate()
        self.report['measuredEvents'].extend([request, dict(request)])
        with self.assertRaisesRegex(ValueError, 'one explicit request'):
            self.validate()

    def test_replacement_cannot_reopen_either_native_source_after_the_gate(self):
        self.replacement()
        original = json.loads(json.dumps(self.report))
        for document in (0, 1, 2):
            self.report = json.loads(json.dumps(original))
            row = next(row for row in self.report['measuredEvents']
                       if row['type'] == 'nativeOpen' and row['documentId'] == document)
            row['startedUs'] = 41
            row['elapsedUs'] = 42
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, 'after registration'):
                self.validate()

    def test_replacement_requires_retired_owner_close_before_gate_release(self):
        self.replacement()
        original = json.loads(json.dumps(self.report))
        for document in (0, 1):
            self.report = json.loads(json.dumps(original))
            row = next(row for row in self.report['measuredEvents']
                       if row['type'] == 'documentEventsClosed' and row['documentId'] == document)
            row['elapsedUs'] = 50
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, 'owners must close'):
                self.validate()

    def test_replacement_requires_pending_registration_and_exactly_two_peer_opens(self):
        self.replacement()
        gate = next(row for row in self.report['measuredEvents'] if row['type'] == 'registrationGateReleased')
        gate['elapsedUs'] = 19
        with self.assertRaisesRegex(ValueError, 'while font registration was pending'):
            self.validate()
        gate['elapsedUs'] = 40
        self.report['measuredEvents'].append({'type': 'nativeOpen', 'documentId': 3,
            'path': str(self.root / 'blank.pdf'), 'pageCount': 1, 'startedUs': 27, 'elapsedUs': 28})
        self.report['allEvents'].append({'type': 'documentEventsClosed', 'documentId': 3, 'elapsedUs': 80})
        with self.assertRaisesRegex(ValueError, 'two intentional peer opens'):
            self.validate()

    def test_replacement_ready_frame_must_belong_to_the_new_live_owner(self):
        self.replacement()
        frame = self.report['measuredFrames'][0]
        frame['documentId'] = 0
        sidecar = json.loads((self.root / 'frame-receipts.json').read_text())
        sidecar['measured'] = self.report['measuredFrames']
        runner.save(self.root / 'frame-receipts.json', sidecar)
        with self.assertRaisesRegex(ValueError, 'painted-ready replacement owner'):
            self.validate()

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

    def test_view_change_during_capture_is_not_a_first_good_frame(self):
        self.report["measuredFrames"][0]["stableView"] = False
        sidecar = json.loads((self.root / "frame-receipts.json").read_text())
        sidecar["measured"] = self.report["measuredFrames"]
        runner.save(self.root / "frame-receipts.json", sidecar)
        with self.assertRaisesRegex(ValueError, "First-good"):
            self.validate()

    def test_reference_requires_a_stable_painted_ready_target(self):
        original = json.loads((self.root / "frame-receipts.json").read_text())
        for key, value in (("renderState", "pending"), ("renderStateAfterCapture", "pending"),
                           ("stableView", False), ("stableDocument", False), ("pageNumber", 2)):
            sidecar = json.loads(json.dumps(original))
            sidecar["reference"][0][key] = value
            runner.save(self.root / "frame-receipts.json", sidecar)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "Reference oracle"):
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

    def test_explicit_standard14_resource_can_report_its_own_query(self):
        original = self.report["measuredEvents"][2]
        query = {**original["queries"][0], "face": "Helvetica"}
        self.report["measuredEvents"].append({**original, "queries": [query], "byPage": {"1": [query]}})
        self.assertEqual(self.validate()["misattributedEvents"], 0)

    def test_secondary_font_without_actual_resource_is_rejected(self):
        original = self.report["measuredEvents"][2]
        query = {**original["queries"][0], "face": "ArbitraryFallback"}
        self.report["measuredEvents"].append({**original, "queries": [query]})
        with self.assertRaisesRegex(ValueError, "wrong-document"):
            self.validate()

    def test_manifest_cannot_invent_a_font_resource(self):
        self.config["documents"][0]["fontResourcesByPage"]["1"].append("Invented")
        with self.assertRaisesRegex(ValueError, "source-grounded manifest"):
            self.validate()

    def test_font_requests_keep_all_five_fields_and_are_not_successes(self):
        original = self.report["measuredEvents"][2]
        query = {**original["queries"][0], "charset": 177}
        self.report["measuredEvents"].append({**original, "queries": [query]})
        result = self.validate()
        self.assertEqual(len(result["distinctFiveFieldRequests"]), 2)
        self.assertEqual(len(result["requestsWithoutObservedSuccessfulFaceRegistration"]), 2)
        self.assertEqual(result["successfulFontRegistrations"], 0)

    def test_incomplete_font_query_is_rejected(self):
        del self.report["measuredEvents"][2]["queries"][0]["charset"]
        with self.assertRaisesRegex(ValueError, "five-field"):
            self.validate()

    def test_capture_cannot_backdate_completed_observation(self):
        self.report["measuredFrames"][0]["captureStartedUs"] = 51
        sidecar = json.loads((self.root / "frame-receipts.json").read_text())
        sidecar["measured"] = self.report["measuredFrames"]
        runner.save(self.root / "frame-receipts.json", sidecar)
        with self.assertRaisesRegex(ValueError, "completion timestamp"):
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
                runner.fixture_font_resources({**item, "absolutePath": str(path)})
                for page in reader.pages:
                    for font in page["/Resources"].get("/Font", {}).values():
                        descriptor = font.get_object().get("/FontDescriptor", {})
                        self.assertFalse({"/FontFile", "/FontFile2", "/FontFile3"} & set(descriptor))


if __name__ == "__main__":
    unittest.main()

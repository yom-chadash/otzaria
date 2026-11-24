"""Transport and partial-result contracts; no Flutter or native build is started."""

import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import internal_apple_shards as shards


class AppleShardContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "source/Probe.app"
        self.executable = self.bundle / "Contents/MacOS/probe"
        self.executable.parent.mkdir(parents=True)
        self.executable.write_bytes(b"test executable identity\n")
        self.executable.chmod(0o755)
        self.framework = self.bundle / "Contents/Frameworks/F.framework"
        (self.framework / "Versions/A").mkdir(parents=True)
        (self.framework / "Versions/A/F").write_bytes(b"test framework identity\n")
        (self.framework / "Versions/Current").symlink_to("A")
        (self.framework / "F").symlink_to("Versions/Current/F")

    def package(self, name="bundle.tar.gz"):
        archive = self.root / name
        return archive, shards.pack_bundle(self.bundle, archive)

    def test_framework_symlinks_and_executable_modes_survive_transport(self):
        archive, inventory = self.package()
        restored = shards.unpack_bundle(archive, self.root / "runtime", inventory, self.bundle.name)
        self.assertEqual(shards.bundle_inventory(restored), inventory)
        self.assertEqual((restored / "Contents/MacOS/probe").stat().st_mode & 0o777, 0o755)
        self.assertTrue((restored / "Contents/Frameworks/F.framework/F").is_symlink())
        self.assertEqual((restored / "Contents/Frameworks/F.framework/F").read_bytes(), b"test framework identity\n")

    def test_repeated_packaging_is_byte_identical(self):
        first, _ = self.package("first.tar.gz")
        second, _ = self.package("second.tar.gz")
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_transport_cannot_substitute_files_after_the_original_build_receipt(self):
        _, inventory = self.package()
        original = {"bundleFiles": {name.removeprefix(self.bundle.name + "/"): item["sha256"]
                                    for name, item in inventory.items() if item["kind"] == "file"}}
        shards.verify_build_inventory(inventory, self.bundle.name, original)
        inventory["Probe.app/Contents/MacOS/probe"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "original build receipt"):
            shards.verify_build_inventory(inventory, self.bundle.name, original)

    def test_escape_in_member_inventory_is_rejected_before_writing(self):
        archive, inventory = self.package()
        inventory["../escape"] = {"kind": "file", "mode": 0o644, "bytes": 0, "sha256": "0" * 64}
        output = self.root / "runtime"
        with self.assertRaisesRegex(ValueError, "Unsafe bundle member"):
            shards.unpack_bundle(archive, output, inventory, self.bundle.name)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "escape").exists())

    def test_escaping_symlink_cannot_be_packaged(self):
        outside = self.root / "outside"
        outside.write_text("unrelated")
        (self.bundle / "Contents/escape").symlink_to("../../../outside")
        with self.assertRaisesRegex(ValueError, "symlink escapes"):
            self.package()
        self.assertEqual(outside.read_text(), "unrelated")

    def test_file_beneath_symlink_is_rejected_before_writing(self):
        archive, inventory = self.package()
        inventory["Probe.app/Contents/Frameworks/F.framework/Versions/Current/evil"] = {
            "kind": "file", "mode": 0o644, "bytes": 0, "sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "non-directory parent"):
            shards.unpack_bundle(archive, self.root / "runtime", inventory, self.bundle.name)

    def altered_archive(self, *, corrupt=False, duplicate=False, hardlink=False):
        archive, inventory = self.package()
        changed = self.root / "changed.tar.gz"
        with tarfile.open(archive, "r:gz") as source, tarfile.open(changed, "w:gz") as target:
            for entry in source.getmembers():
                payload = source.extractfile(entry).read() if entry.isfile() else None
                if entry.name.endswith("/probe"):
                    if corrupt:
                        payload = b"x" + payload[1:]
                    if hardlink:
                        entry.type, entry.linkname, entry.size = tarfile.LNKTYPE, "Probe.app/foreign", 0
                        payload = None
                target.addfile(entry, io.BytesIO(payload) if payload is not None else None)
                if duplicate and entry.name.endswith("/probe"):
                    target.addfile(entry, io.BytesIO(payload))
        return changed, inventory

    def test_corrupted_payload_is_not_accepted_with_unchanged_inventory(self):
        archive, inventory = self.altered_archive(corrupt=True)
        with self.assertRaisesRegex(ValueError, "digest differs"):
            shards.unpack_bundle(archive, self.root / "runtime", inventory, self.bundle.name)

    def test_duplicate_archive_members_are_rejected(self):
        archive, inventory = self.altered_archive(duplicate=True)
        with self.assertRaisesRegex(ValueError, "membership differs"):
            shards.unpack_bundle(archive, self.root / "runtime", inventory, self.bundle.name)

    def test_hardlink_is_rejected(self):
        archive, inventory = self.altered_archive(hardlink=True)
        with self.assertRaisesRegex(ValueError, "hard-link differs"):
            shards.unpack_bundle(archive, self.root / "runtime", inventory, self.bundle.name)

    def test_producer_receipt_digest_is_required_before_extraction(self):
        prepared = self.root / "prepared"
        prepared.mkdir()
        (prepared / "transport.json").write_text("{}")
        args = SimpleNamespace(prepared=prepared, expected_transport_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "receipt differs"):
            shards.load_prepared(args, self.root / "never-created")
        self.assertFalse((self.root / "never-created").exists())

    def make_baseline(self, workload):
        output = self.root / workload
        output.mkdir()
        args = SimpleNamespace(document="uxlc-genesis", workload=workload, expected_transport_sha256="a" * 64,
                               corpus_manifest=self.root / "corpus.json")
        context = ({}, {}, [{"id": args.document, "path": "input.pdf", "sha256": "b" * 64}],
                   self.bundle, self.executable)
        return args, output, context

    def fake_measurement(self, backends, *, fail_at=None):
        def run(command, cwd, log, timeout, extra_env):
            self.assertEqual(command[:2], ["/usr/bin/time", "-lp"])
            self.assertEqual(timeout, 600)
            config = json.loads(Path(extra_env["ISSUE5538_CONFIG"]).read_text())
            backends.append(config["backend"])
            if len(backends) == fail_at:
                raise TimeoutError("synthetic owned-process timeout")
            count = 20 if config["workload"] == "lifecycle" else 3
            report = {key: config[key] for key in ("backend", "workload", "inputSha256")}
            report.update(measurementComplete=True, errors=[], documentsOpened=count, documentsClosed=count,
                          observations=[{"pixelWidth": 1, "pixelHeight": 1, "pageCount": 1} for _ in range(count)],
                          frameTimings=[{"buildUs": 1}])
            shards.save(Path(config["output"]), report)
            return "real 1.0\nuser 0.1\nsys 0.1\n123 maximum resident set size\n"
        return run

    def test_complete_block_keeps_all_four_processes_and_twenty_lifecycles(self):
        args, output, context = self.make_baseline("lifecycle")
        backends = []
        with patch.object(shards, "verify_after"), patch.object(shards.producer, "run", side_effect=self.fake_measurement(backends)):
            shards.baseline(args, output, context)
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(backends, ["pdfium", "coregraphics", "coregraphics", "pdfium"])
        self.assertTrue(summary["measurementComplete"])
        self.assertEqual(summary["completedProcesses"], 4)
        self.assertEqual(summary["observationsPerProcess"], 20)
        self.assertFalse(summary["timingsPooledAcrossHosts"])

    def test_timeout_keeps_partial_rows_without_whole_block_success(self):
        args, output, context = self.make_baseline("file")
        backends = []
        with patch.object(shards, "verify_after"), patch.object(shards.producer, "run", side_effect=self.fake_measurement(backends, fail_at=3)):
            with self.assertRaises(TimeoutError):
                shards.baseline(args, output, context)
        summary = json.loads((output / "summary.json").read_text())
        self.assertFalse(summary["measurementComplete"])
        self.assertFalse(summary["binaryVerifiedAfterBlock"])
        self.assertEqual((summary["completedProcesses"], summary["expectedProcesses"]), (2, 4))
        self.assertEqual(json.loads((output / "phase-progress.json").read_text())["completedProcesses"], 2)
        self.assertIn("Restart the complete", summary["resumePolicy"])

    def test_post_block_identity_failure_cannot_claim_complete_comparison(self):
        args, output, context = self.make_baseline("file")
        backends = []
        with patch.object(shards, "verify_after", side_effect=[None] * 4 + [ValueError("changed bundle")]), \
                patch.object(shards.producer, "run", side_effect=self.fake_measurement(backends)):
            with self.assertRaisesRegex(ValueError, "changed bundle"):
                shards.baseline(args, output, context)
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["completedProcesses"], 4)
        self.assertFalse(summary["measurementComplete"])
        self.assertFalse(summary["binaryVerifiedAfterBlock"])


if __name__ == "__main__":
    unittest.main()

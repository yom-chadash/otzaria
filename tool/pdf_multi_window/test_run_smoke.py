"""Correctness evidence must reject stale or substituted native artifacts."""

import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tool.pdf_multi_window.engine import (
    BundledFlutterEngine, bundle_identity, darwin_engine_images, linux_engine_images,
    verify_bundle_unchanged,
)
from tool.pdf_multi_window.run_smoke import (
    OwnedProcess, engine_identity, file_sha, source_snapshot, validate_completion,
)


class SmokeEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.receipt = {"sourceSha": "a" * 40, "buildMode": "profile",
                        "engineIdentity": {"bundleSha256": "b" * 64},
                        "engineRevision": "c" * 40}
        self.report = self.root / "report.json"
        self.report.write_text(json.dumps({"steps": []}), encoding="utf-8")
        self.completion = {"schemaVersion": 1, "status": "passed", **self.receipt,
                           "bindingClass": "WidgetsFlutterBinding", "nativePlatformDispatcher": True,
                           "unhandledErrors": [], "error": None, "reportSha256": file_sha(self.report)}

    def write_completion(self, value=None):
        (self.root / "completion.json").write_text(json.dumps(value or self.completion), encoding="utf-8")

    def test_valid_completion_requires_exact_source_engine_and_report(self):
        self.write_completion()
        self.assertEqual(validate_completion(self.root, self.receipt), self.completion)

    def test_stale_source_or_failed_binding_cannot_pass(self):
        for name, value in (("sourceSha", "d" * 40), ("engineRevision", "e" * 40),
                            ("engineIdentity", {}), ("bindingClass", "IntegrationTestWidgetsFlutterBinding"),
                            ("nativePlatformDispatcher", False), ("status", "failed"),
                            ("schemaVersion", True), ("unhandledErrors", ["late Flutter error"]),
                            ("error", "assertion failed")):
            with self.subTest(name=name):
                completion = copy.deepcopy(self.completion)
                completion[name] = value
                self.write_completion(completion)
                with self.assertRaises(RuntimeError):
                    validate_completion(self.root, self.receipt)

    def test_report_replacement_after_completion_is_rejected(self):
        self.write_completion()
        self.report.write_text('{"status":"replaced"}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "report has changed"):
            validate_completion(self.root, self.receipt)

    def test_dirty_source_is_rejected_before_build(self):
        with patch("tool.pdf_multi_window.run_smoke.capture", side_effect=["a" * 40, " M pubspec.lock"]):
            with self.assertRaisesRegex(RuntimeError, "clean source"):
                source_snapshot(root=self.root)

    def test_another_commit_is_rejected_even_when_clean(self):
        with patch("tool.pdf_multi_window.run_smoke.capture", side_effect=["d" * 40, ""]):
            with self.assertRaisesRegex(RuntimeError, "source commit changed"):
                source_snapshot("a" * 40, root=self.root)

    def test_windows_release_requires_matching_patched_binary(self):
        executable = self.root / "otzaria.exe"
        executable.write_bytes(b"exe")
        engine = self.root / "flutter_windows.dll"
        engine.write_bytes(b"patched engine")
        with self.assertRaisesRegex(RuntimeError, "requires the patched"):
            engine_identity(executable, "windows", "release", "a" * 40)
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            engine_identity(executable, "windows", "release", "a" * 40, "b" * 64)
        identity = engine_identity(executable, "windows", "release", "a" * 40, file_sha(engine))
        self.assertEqual(identity["verification"], "patched-action-sha256")

    def test_replacing_a_bundled_posix_engine_is_rejected(self):
        executable = self.root / "otzaria"
        executable.write_bytes(b"exe")
        engine = self.root / "lib/libflutter_linux_gtk.so"
        engine.parent.mkdir()
        engine.write_bytes(b"engine A")
        guard = BundledFlutterEngine(executable, "linux", "profile")
        engine.write_bytes(b"engine B")
        with self.assertRaisesRegex(RuntimeError, "bytes changed"):
            guard.verify_unchanged()

    def test_deleted_linux_mapping_is_not_accepted(self):
        with self.assertRaisesRegex(RuntimeError, "deleted"):
            linux_engine_images("10-20 r-xp 0000 08:01 42 /bundle/libflutter_linux_gtk.so (deleted)",
                                "libflutter_linux_gtk.so")

    def test_mac_mapping_must_belong_to_the_owned_pid(self):
        with self.assertRaisesRegex(RuntimeError, "another process"):
            darwin_engine_images("p999\0ftxt\0n/bundle/FlutterMacOS\0", 123, "FlutterMacOS")

    def test_cleanup_terminates_only_its_owned_child(self):
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        unrelated = subprocess.Popen(command)
        try:
            with (self.root / "child.log").open("w", encoding="utf-8") as log:
                child = OwnedProcess(command, log=log)
                child.close()
                self.assertIsNotNone(child.process.poll())
                self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=10)

    @unittest.skipIf(os.name == "nt", "POSIX waitable-child ownership")
    def test_exit_is_observed_without_reaping_until_all_group_signals_finish(self):
        actual_killpg = os.killpg
        signals = []
        with (self.root / "exited-child.log").open("w", encoding="utf-8") as log:
            child = OwnedProcess([sys.executable, "-c", "pass"], log=log)
            try:
                self.assertEqual(child.wait(timeout=5), 0)
                self.assertIsNone(child.process.returncode)

                def guarded_signal(pid, signum):
                    # A real waitable zombie reserves this PID/PGID at every signal.
                    result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    self.assertIsNotNone(result)
                    self.assertIsNone(child.process.returncode)
                    signals.append(signum)
                    actual_killpg(pid, signum)

                with patch("tool.pdf_multi_window.run_smoke.os.killpg", side_effect=guarded_signal):
                    child.close()
                    child.close()  # No later signal may use the reaped PID/PGID.
                self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
                self.assertEqual(child.process.returncode, 0)
                with self.assertRaises(ChildProcessError):
                    os.waitid(os.P_PID, child.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            finally:
                child.close()

    @unittest.skipIf(os.name == "nt", "POSIX waitable-child ownership")
    def test_external_reap_refuses_all_group_signals(self):
        with (self.root / "reaped-child.log").open("w", encoding="utf-8") as log:
            child = OwnedProcess([sys.executable, "-c", "pass"], log=log)
            child.process.wait(timeout=5)
            with patch("tool.pdf_multi_window.run_smoke.os.killpg") as kill_group:
                with self.assertRaisesRegex(RuntimeError, "already reaped"):
                    child.close()
                kill_group.assert_not_called()

    def make_bundle(self, system):
        directory = self.root / system
        if system == "macos":
            directory /= "Smoke.app"
            executable = directory / "Contents/MacOS/otzaria"
            payload = directory / "Contents/Frameworks/App.framework/App"
            engine = directory / "Contents/Frameworks/FlutterMacOS.framework/FlutterMacOS"
        else:
            executable = directory / ("otzaria.exe" if system == "windows" else "otzaria")
            payload = directory / ("data/app.so" if system == "windows" else "lib/libapp.so")
            engine = directory / ("flutter_windows.dll" if system == "windows" else "lib/libflutter_linux_gtk.so")
        for path, contents in ((executable, b"unchanged launcher"), (engine, b"unchanged engine"),
                               (payload, b"compiled Dart AOT v1")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        return executable, payload

    def test_payload_only_replacement_is_rejected_on_all_platform_layouts(self):
        for system in ("linux", "macos", "windows"):
            with self.subTest(system=system):
                executable, payload = self.make_bundle(system)
                original = bundle_identity(executable, system)
                launcher_hash = file_sha(executable)
                payload.write_bytes(b"different compiled Dart AOT v2")
                self.assertEqual(file_sha(executable), launcher_hash)
                with self.assertRaisesRegex(RuntimeError, "Dart AOT payload changed"):
                    verify_bundle_unchanged(executable, system, original)

    def test_launcher_and_engine_without_aot_payload_cannot_be_verified(self):
        executable, payload = self.make_bundle("linux")
        payload.unlink()
        with self.assertRaisesRegex(RuntimeError, "AOT payload is missing"):
            bundle_identity(executable, "linux")

    def test_added_asset_is_detected_even_when_all_native_code_is_unchanged(self):
        executable, _ = self.make_bundle("windows")
        original = bundle_identity(executable, "windows")
        (executable.parent / "data/new-asset.txt").write_text("changed runtime input", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "app bundle"):
            verify_bundle_unchanged(executable, "windows", original)

    @unittest.skipIf(os.name == "nt", "Framework symlink layout needs POSIX link support")
    def test_versioned_mac_framework_payload_is_included_in_manifest(self):
        executable, payload = self.make_bundle("macos")
        versioned = payload.parent / "Versions/A/App"
        versioned.parent.mkdir(parents=True)
        payload.rename(versioned)
        (payload.parent / "Versions/Current").symlink_to("A", target_is_directory=True)
        payload.symlink_to("Versions/Current/App")
        original = bundle_identity(executable, "macos")
        self.assertEqual(original["aotPayload"]["sha256"], file_sha(versioned))
        self.assertIn("Versions/A/App", original["aotPayload"]["resolved"])
        verify_bundle_unchanged(executable, "macos", original)
        versioned.write_bytes(b"substituted AOT behind the same framework links")
        with self.assertRaisesRegex(RuntimeError, "Dart AOT payload changed"):
            verify_bundle_unchanged(executable, "macos", original)

    @unittest.skipIf(os.name == "nt", "Symlink boundary needs POSIX link support")
    def test_aot_payload_link_outside_bundle_is_rejected(self):
        executable, payload = self.make_bundle("linux")
        outside = self.root / "external-aot.so"
        payload.rename(outside)
        payload.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "outside the tested app bundle"):
            bundle_identity(executable, "linux")


if __name__ == "__main__":
    unittest.main()

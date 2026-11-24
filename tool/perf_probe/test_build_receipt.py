import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import build_receipt as receipt
import run_pdf_pair as pair


class FrozenBuildTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Receipt fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.write(self.repo / ".gitignore", "build/\n.dart_tool/\n")
        self.write(self.repo / "pubspec.yaml", "name: fixture\n")
        self.write(self.repo / "pubspec.lock", "locked fixture\n")
        self.target = self.repo / "tool/perf_probe/framework_pdf_probe.dart"
        self.workload = self.repo / "integration_test/support/perf_resource_workload.dart"
        self.write(self.target, "void main() {}\n")
        self.write(self.workload, "const workload = 'real target fixture';\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Frozen build fixture")
        self.write(self.repo / ".dart_tool/package_config.json", '{"packages":[]}\n')
        self.write(self.repo / ".dart_tool/package_graph.json", '{"roots":["fixture"]}\n')
        self.sdk = self.root / "sdk"
        self.system = "windows" if os.name == "nt" else "linux"
        self.sdk_tool = self.sdk / ("bin/flutter.bat" if os.name == "nt" else "bin/flutter")
        self.sdk_input = self.sdk / "lib/a compiler input.dart"
        self.write(self.sdk_tool, "SDK compiler fixture\n")
        self.write(self.sdk_input, "SDK source fixture\n")
        self.aot_compiler = self.sdk / ("bin/cache/artifacts/engine/windows-x64/gen_snapshot.exe" if os.name == "nt"
                                       else "bin/cache/artifacts/engine/linux-x64/gen_snapshot")
        self.write(self.aot_compiler, "AOT compiler fixture\n")
        self.spec = {"repository": str(self.repo), "platform": self.system, "mode": "profile", "architecture": "x86_64",
                     "binding": receipt.binding_identity("framework"), "expectedPatchedWindowsEngineSha256": "",
                     "buildEnvironment": {}}
        self.sdk_mock = patch.object(receipt, "sdk_identity", side_effect=self.sdk_identity)
        self.sdk_mock.start()
        self.addCleanup(self.sdk_mock.stop)
        self.executable = self.repo / ("build/windows/x64/runner/Profile/otzaria.exe" if os.name == "nt"
                                       else "build/linux/x64/profile/bundle/otzaria")
        self.bundle = self.executable.parent
        self.write(self.executable, "launcher fixture\n")
        self.executable.chmod(0o755)
        self.aot = self.bundle / ("data/app.so" if os.name == "nt" else "lib/libapp.so")
        self.native_engine = self.bundle / ("flutter_windows.dll" if os.name == "nt" else "lib/libflutter_linux_gtk.so")
        self.write(self.aot, "real AOT payload fixture\n")
        self.write(self.native_engine, "Flutter engine fixture\n")
        self.asset = self.bundle / "data/flutter_assets/manifest.json"
        self.write(self.asset, '{"asset":"frozen"}\n')
        self.depfile = self.repo / ".dart_tool/flutter_build/target/kernel_snapshot_program.d"
        escaped = lambda path: str(path).replace("\\", "\\\\").replace(" ", "\\ ")
        self.write(self.depfile, f"program.dill: {escaped(self.target)} {escaped(self.workload)} {escaped(self.sdk_input)}\n")
        self.frozen = self.root / "build-receipts/baseline.json"
        self.before = receipt.prepare(self.spec, self.frozen)
        self.sha = receipt.freeze(self.spec, self.frozen, self.before, self.executable)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def sdk_identity(self, _repository):
        return {"root": str(self.sdk), "identity": {"frameworkRevision": receipt.FRAMEWORK_REVISION},
                "tools": [receipt.file_record(self.sdk_tool)]}

    def verify(self, spec=None):
        return receipt.verify(spec or self.spec, self.frozen, self.sha, self.executable)

    def test_valid_reuse_preserves_aot_and_has_no_build_or_pub_get_path(self):
        real_capture = receipt.capture
        commands = []
        def capture(command, *args, **kwargs):
            commands.append(command)
            return real_capture(command, *args, **kwargs)
        with patch.object(receipt, "capture", side_effect=capture):
            proof = self.verify()
        self.assertEqual(proof["receiptSha256"], self.sha)
        self.assertEqual(proof["aotPayload"]["sha256"], receipt.file_identity(self.aot)["sha256"])
        self.assertFalse(proof["verificationOverlapsAppSampling"])
        self.assertTrue(all(command[0] == "git" for command in commands))
        self.assertFalse(any("build" in command or "pub" in command for command in commands))

    def test_receipt_cannot_be_replaced_or_reused_after_modification(self):
        original = self.frozen.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "existing"):
            receipt.prepare(self.spec, self.frozen)
        with self.assertRaisesRegex(RuntimeError, "existing"):
            receipt.freeze(self.spec, self.frozen, self.before, self.executable)
        self.assertEqual(self.frozen.read_bytes(), original)
        self.frozen.write_bytes(original + b" ")
        with self.assertRaisesRegex(RuntimeError, "receipt bytes changed"):
            self.verify()

    def test_symlink_receipt_is_rejected(self):
        alias = self.root / "alias.json"
        try:
            alias.symlink_to(self.frozen)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Creating symlinks needs a Windows privilege; regular-file receipt guards still run")
            raise
        with self.assertRaisesRegex(RuntimeError, "existing|symlink"):
            receipt.prepare(self.spec, alias)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            receipt.verify(self.spec, alias, self.sha, self.executable)

    def test_missing_receipt_is_rejected(self):
        self.frozen.unlink()
        with self.assertRaises(FileNotFoundError):
            self.verify()

    def test_dirty_or_different_source_is_rejected_without_rebuild(self):
        self.write(self.target, "changed target\n")
        with self.assertRaisesRegex(RuntimeError, "clean source"):
            self.verify()
        self.git("add", ".")
        self.git("commit", "-qm", "Different source")
        with self.assertRaisesRegex(RuntimeError, "Frozen source"):
            self.verify()

    def test_sdk_and_package_resolution_drift_are_rejected(self):
        for path in (self.sdk_tool, self.repo / ".dart_tool/package_config.json", self.repo / ".dart_tool/package_graph.json"):
            with self.subTest(path=path):
                original = path.read_text()
                self.write(path, original + "changed")
                with self.assertRaisesRegex(RuntimeError, "Frozen source"):
                    self.verify()
                self.write(path, original)

    def test_dependency_source_and_aot_compiler_drift_are_rejected(self):
        for path in (self.sdk_input, self.aot_compiler):
            with self.subTest(path=path):
                original = path.read_text()
                self.write(path, original + "changed")
                with self.assertRaisesRegex(RuntimeError, "compiler|compiler inputs"):
                    self.verify()
                self.write(path, original)

    def test_aot_native_and_assets_are_checked_even_if_launcher_is_unchanged(self):
        launcher = receipt.file_identity(self.executable)
        for path in (self.aot, self.native_engine, self.asset):
            with self.subTest(path=path):
                original = path.read_text()
                self.write(path, original + "changed")
                with self.assertRaisesRegex(RuntimeError, "AOT, native binary, assets"):
                    self.verify()
                self.assertEqual(receipt.file_identity(self.executable), launcher)
                self.write(path, original)

    def test_added_asset_is_rejected(self):
        added = self.bundle / "data/flutter_assets/late.txt"
        self.write(added, "late asset")
        with self.assertRaisesRegex(RuntimeError, "assets"):
            self.verify()

    def test_escaping_bundle_symlink_is_rejected(self):
        added = self.bundle / "data/flutter_assets/late.txt"
        try:
            added.symlink_to(self.sdk_input)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Creating symlinks needs a Windows privilege; asset and payload drift guards still run")
            raise
        with self.assertRaisesRegex(RuntimeError, "outside"):
            self.verify()

    def test_mode_target_flags_cannot_share_a_receipt(self):
        for mutate in (
            lambda spec: spec.update(mode="release"),
            lambda spec: spec["binding"].update(probeTarget="integration_test/other.dart"),
            lambda spec: spec["buildEnvironment"].update(CFLAGS="-different"),
        ):
            spec = copy.deepcopy(self.spec)
            mutate(spec)
            with self.assertRaisesRegex(RuntimeError, "flags or target"):
                self.verify(spec)

    def test_change_during_build_cannot_be_frozen(self):
        before = copy.deepcopy(self.before)
        before["inputs"]["command"]["arguments"].append("--different-build")
        path = self.root / "other-receipt.json"
        with self.assertRaisesRegex(RuntimeError, "changed while building"):
            receipt.freeze(self.spec, path, before, self.executable)
        self.assertFalse(path.exists())


class ReceiptArgumentTest(unittest.TestCase):
    def test_flutter_depfile_escaping_keeps_windows_paths_spaces_and_quotes(self):
        self.assertEqual(receipt.depfile_paths(r'''C:\\work\\a\ b.dart C:\\work\\quote'file.dart /root/a\ b.dart'''),
                         ["C:\\work\\a b.dart", "C:\\work\\quote'file.dart", "/root/a b.dart"])

    def test_native_reuse_arguments_require_external_hash_and_carry_python_on_windows(self):
        import argparse
        args = argparse.Namespace(mode="profile", cache_state="cold", scenario="pdf-reader",
                                  launches=3, repetitions=5, expected_engine_sha256="", display_mode="desktop")
        options = dict(build_receipt=Path("frozen.json"), build_policy="reuse", build_receipt_sha256="a" * 64)
        windows = pair.native_arguments(Path("checkout"), args, Path("output"), Path("case.json"), "host", "Windows", "pwsh", **options)
        self.assertIn("-BuildReceiptPython", windows)
        self.assertIn("-BuildReceiptSha256", windows)
        linux = pair.native_arguments(Path("checkout"), args, Path("output"), Path("case.json"), "host", "Linux", **options)
        self.assertIn("--build-receipt-sha256", linux)
        with self.assertRaisesRegex(ValueError, "pinned"):
            pair.native_arguments(Path("checkout"), args, Path("output"), Path("case.json"), "host", "Linux",
                                  build_receipt=Path("frozen.json"), build_policy="reuse")


if __name__ == "__main__":
    unittest.main()

"""Compare both upstream Apple backends in one isolated Flutter 3.50 binary."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time
import traceback

import internal_apple_trace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "perf_probe"))
from run_perf_probe_posix import child_exit, cleanup_group


UPSTREAM_SHA = "683df93e986af19bbcefd22606a417083116847b"
FLUTTER_SHA = "ab2912035e160d9cad6306df307576da6128ebdb"
ENGINE_SHA = "371f77202a73e8dff57a8b3bff75bca55c090aee"
SWIFT_PLUGIN = "packages/pdfrx_coregraphics/darwin/pdfrx_coregraphics/Sources/PdfrxCoregraphicsPlugin.swift"


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def run(command, directory, log, timeout=900, extra_env=None):
    command = [str(x) for x in command]
    receipt = {"command": command, "timeoutSeconds": timeout, "timedOut": False, "commandCompleted": False,
               "ownedProcessGroupReaped": False}
    with log.open("x", encoding="utf-8") as output:
        process = subprocess.Popen(command, cwd=directory,
            env={**os.environ, "CI": "true", **(extra_env or {})},
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        receipt["processGroupPid"] = process.pid
        try:
            deadline = time.monotonic() + timeout
            while (code := child_exit(process)) is None:
                if time.monotonic() >= deadline:
                    receipt["timedOut"] = True
                    raise TimeoutError(f"Command exceeded {timeout}s; partial log: {log}")
                time.sleep(.05)
            receipt["exitCode"] = code
            receipt["commandCompleted"] = True
        finally:
            try:
                cleanup_group(process)
                receipt["ownedProcessGroupReaped"] = True
            finally:
                write_json(log.with_suffix(".process.json"), receipt)
    result = log.read_text(encoding="utf-8")
    require(code == 0, f"Command exited {code}: {command}\n{result[-5000:]}")
    return result


def validate(report, config):
    require(report.get("measurementComplete") is True and report.get("errors") == [], "Apple observation did not finish")
    for key in ("backend", "workload", "inputSha256"):
        require(report.get(key) == config[key], f"Apple input/backend identity mismatch: {key}")
    expected = 20 if config["workload"] == "lifecycle" else 3
    require(report.get("documentsOpened") == expected and report.get("documentsClosed") == expected, "Unclosed Apple document")
    require(len(report.get("observations", [])) == expected and report.get("frameTimings"), "Missing Apple timing samples")
    require(all(r["pixelWidth"] > 0 and r["pixelHeight"] > 0 and r["pageCount"] > 0 for r in report["observations"]), "Missing real raster")
    if config["workload"] == "custom":
        require(report.get("customDisposeCallbacks") == expected, "Custom reader disposal mismatch")
        require(all(r["customReadBytes"] > 0 for r in report["observations"]), "Custom reader was not exercised")


def apply_swift_initializer_compatibility(upstream, output):
    path = upstream / SWIFT_PLUGIN
    original = subprocess.check_output(["git", "show", f"{UPSTREAM_SHA}:{SWIFT_PLUGIN}"], cwd=upstream, timeout=60)
    require(path.read_bytes() == original, "CoreGraphics Swift source changed before the compatibility patch")
    before = original.decode("utf-8")
    after = before
    for variable, constructor, code, message in (
        ("pdfDocument", "PDFDocument", "pdf-document-failure", "Failed to create empty PDFDocument."),
        ("pdfDocument", "PDFDocument", "pdf-document-failure", "Failed to create PDFDocument."),
        ("pdfPage", "PDFPage", "pdf-page-failure", "Failed to create PDF page."),
    ):
        old = f'''    guard let {variable} = {constructor}() else {{
      result(
        FlutterError(
          code: "{code}", message: "{message}", details: nil
        ))
      return
    }}'''
        require(after.count(old) == 1, "CoreGraphics initializer compatibility anchor changed")
        after = after.replace(old, f"    let {variable} = {constructor}()", 1)
    patch = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                       fromfile="a/" + SWIFT_PLUGIN, tofile="b/" + SWIFT_PLUGIN))
    patched = after.encode("utf-8")
    receipt = {
        "upstreamCommit": UPSTREAM_SHA, "path": SWIFT_PLUGIN,
        "sourceRole": "baseline plus initializer-only compile compatibility patch",
        "originalSha256": hashlib.sha256(original).hexdigest(),
        "patchedSha256": hashlib.sha256(patched).hexdigest(),
        "patchSha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(), "patch": patch,
        "changedMethods": ["createNewDocument", "createDocumentFromJpegData"],
        "reason": "PDFDocument() and PDFPage() are non-optional in the tested Apple SDK; conditional binding cannot compile.",
        "renderTextSchedulerChanged": False,
    }
    path.write_bytes(patched)
    write_json(output / "upstream-swift-compatibility.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flutter", type=Path, required=True)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--with-thread-trace", action="store_true",
                        help="After the unprofiled ABBA runs, export separate Time Profiler stacks.")
    args = parser.parse_args()
    require(platform.system() == "Darwin" and os.environ.get("CI") == "true", "Apple producer needs macOS and CI=true")
    require(all(hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT", "CLD_EXITED")),
            "This macOS Python lacks waitable-child process-group ownership support")
    module = Path(__file__).resolve().parent
    repo = module.parents[1]
    output = args.output.resolve()
    require(not output.exists() and not output.is_relative_to(repo), "Apple output must be new and outside source")
    output.mkdir(parents=True)
    try:
        sdk = json.loads(run([args.flutter, "--version", "--machine"], repo, output / "flutter.json"))
        require(sdk.get("frameworkVersion") == "3.50.0" and sdk.get("frameworkRevision") == FLUTTER_SHA
                and sdk.get("engineRevision") == ENGINE_SHA, "Isolated Apple SDK identity changed")
        source = run(["git", "rev-parse", "HEAD"], repo, output / "source-sha.log").strip()
        dirty = run(["git", "status", "--porcelain", "--untracked-files=all"], repo, output / "source-status.log")
        require(not dirty, "Fork source must be clean")
        upstream = output / "upstream"
        upstream.mkdir()
        run(["git", "init"], upstream, output / "git-init.log")
        run(["git", "remote", "add", "origin", "https://github.com/espresso3389/pdfrx.git"], upstream, output / "git-remote.log")
        run(["git", "fetch", "--depth", "1", "origin", UPSTREAM_SHA], upstream, output / "git-fetch.log")
        run(["git", "checkout", "--detach", "FETCH_HEAD"], upstream, output / "checkout.log")
        require(run(["git", "rev-parse", "HEAD"], upstream, output / "upstream-sha.log").strip() == UPSTREAM_SHA, "Upstream identity changed")
        compatibility = apply_swift_initializer_compatibility(upstream, output)
        xcode_version = run(["xcodebuild", "-version"], upstream, output / "xcode-version.log")
        app = upstream / "packages/issue5538_apple"
        run([args.flutter, "create", "--no-pub", "--platforms=macos", "--project-name=issue5538_apple", app], upstream, output / "create.log")
        workspace = upstream / "pubspec.yaml"
        workspace.write_text(workspace.read_text() + "  - packages/issue5538_apple\n")
        (app / "pubspec.yaml").write_text("""name: issue5538_apple
publish_to: none
environment:
  sdk: ^3.14.0
resolution: workspace
dependencies:
  flutter:
    sdk: flutter
  pdfrx: 3.1.0
  pdfrx_coregraphics: 0.5.3
  crypto: ^3.0.7
""")
        shutil.copyfile(module / "internal_apple_probe.dart.template", app / "lib/main.dart")
        shutil.rmtree(app / "test", ignore_errors=True)
        (app / "analysis_options.yaml").write_text("analyzer:\n  errors:\n    depend_on_referenced_packages: error\n")
        for path in (app / "macos/Runner").glob("*.entitlements"):
            content = plistlib.loads(path.read_bytes())
            content["com.apple.security.app-sandbox"] = False
            path.write_bytes(plistlib.dumps(content))
        project = app / "macos/Runner.xcodeproj/project.pbxproj"
        project.write_text(re.sub(r"MACOSX_DEPLOYMENT_TARGET = [0-9.]+;", "MACOSX_DEPLOYMENT_TARGET = 12.0;", project.read_text()))
        podfile = app / "macos/Podfile"
        if podfile.exists():
            podfile.write_text(re.sub(r"platform :osx, '[0-9.]+'", "platform :osx, '12.0'", podfile.read_text()))
        run([args.flutter, "pub", "get"], app, output / "pub-get.log")
        lock = upstream / "pubspec.lock"
        require(lock.exists(), "Missing isolated workspace lock")
        shutil.copyfile(lock, output / "upstream-pubspec.lock")
        run([args.flutter, "pub", "get", "--enforce-lockfile"], app, output / "pub-get-enforced.log")
        run([args.flutter, "analyze", "--no-pub", "lib/main.dart"], app, output / "analyze.log")
        require(digest(upstream / SWIFT_PLUGIN) == compatibility["patchedSha256"], "CoreGraphics compatibility source changed before build")
        run([args.flutter, "build", "macos", "--release", "--no-pub"], app, output / "build.log", timeout=1800)
        require(digest(upstream / SWIFT_PLUGIN) == compatibility["patchedSha256"], "CoreGraphics compatibility source changed during build")
        bundles = list((app / "build/macos/Build/Products/Release").glob("*.app"))
        require(len(bundles) == 1, "Ambiguous Apple app bundle")
        bundle = bundles[0]
        info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
        executable = bundle / "Contents/MacOS" / info["CFBundleExecutable"]
        bundle_hashes = {str(p.relative_to(bundle)): digest(p) for p in bundle.rglob("*") if p.is_file() and not p.is_symlink()}
        corpus = json.loads(args.corpus_manifest.read_text())
        wanted = {"uxlc-genesis", "mikraot-gedolot-genesis-1859", "gcc-15_2"}
        documents = [d for d in corpus["documents"] if d["id"] in wanted]
        require({d["id"] for d in documents} == wanted, "Missing Apple real-corpus inputs")
        generated = [workspace, app / "pubspec.yaml", app / "lib/main.dart", project,
                     *(app / "macos/Runner").glob("*.entitlements")]
        write_json(output / "provenance.json", {"sourceCommit": source, "sourceDirty": False,
            "upstreamCommit": UPSTREAM_SHA, "flutter": sdk, "bundleFiles": bundle_hashes,
            "upstreamSourceRole": compatibility["sourceRole"], "compileCompatibility": compatibility,
            "xcodeVersion": xcode_version.strip(),
            "operatingSystem": platform.platform(), "architecture": platform.machine(),
            "upstreamLockSha256": digest(lock), "corpusManifestSha256": digest(args.corpus_manifest),
            "harnessFiles": {p.name: digest(p) for p in module.glob("internal_apple_*") if p.is_file()},
            "nativeOwnerSupportFiles": {name: digest(module.parent / "perf_probe" / name) for name in (
                "run_perf_probe_posix.py", "posix_engine.py", "posix_resources.py", "probe_binding.py")},
            "generatedOverrides": {str(p.relative_to(upstream)): digest(p) for p in generated},
            "productionSdkChanged": False, "otzariaPerformanceComparison": False,
            "runOrder": ["pdfium", "coregraphics", "coregraphics", "pdfium"]})
        runs = []
        baseline_error = None
        try:
            for document in documents:
                path = (args.corpus_manifest.parent / document["path"]).resolve()
                require(digest(path) == document["sha256"], "Apple corpus hash mismatch")
                for workload in ("file", "custom", "lifecycle"):
                    for index, backend in enumerate(("pdfium", "coregraphics", "coregraphics", "pdfium")):
                        label = f"{document['id']}-{workload}-{index + 1}-{backend}"
                        config = {"backend": backend, "workload": workload, "input": str(path),
                                  "inputSha256": document["sha256"], "output": str(output / f"{label}.json")}
                        config_path = output / f"{label}-config.json"
                        write_json(config_path, config)
                        usage = run(["/usr/bin/time", "-lp", executable], app, output / f"{label}.log", timeout=600,
                                    extra_env={"ISSUE5538_CONFIG": str(config_path)})
                        report = json.loads(Path(config["output"]).read_text())
                        validate(report, config)
                        cpu = {name: float(value) for name, value in re.findall(r"(?m)^(real|user|sys)\s+([\d.]+)$", usage)}
                        peak = re.search(r"(?m)^\s*(\d+)\s+maximum resident set size\s*$", usage)
                        require(set(cpu) == {"real", "user", "sys"} and peak, "Missing macOS process resource usage")
                        runs.append({"label": label, "reportSha256": digest(config["output"]),
                                     "processSeconds": cpu, "processPeakRssBytes": int(peak.group(1))})
                        write_json(output / "runs.json", runs)
        except TimeoutError as error:
            cleanup = json.loads((output / f"{label}.process.json").read_text())
            require(cleanup.get("timedOut") is True and cleanup.get("ownedProcessGroupReaped") is True,
                    "Timed-out workload was not safely closed; do not start profiling")
            baseline_error = error
            write_json(output / "baseline-failure.json", {"measurementComplete": False,
                "label": label, "completedProcesses": len(runs), "expectedProcesses": 36,
                "error": traceback.format_exc(), "cleanupReceipt": cleanup,
                "traceMayContinueAfterBundleVerification": True})
        require({str(p.relative_to(bundle)): digest(p) for p in bundle.rglob("*") if p.is_file() and not p.is_symlink()} == bundle_hashes,
                "Apple binary changed between paired runs")
        write_json(output / "summary.json", {"measurementComplete": baseline_error is None, "section": "E12", "runs": runs,
            "completedProcesses": len(runs), "expectedProcesses": 36, "bundleVerifiedAfterBaseline": True,
            "scope": "isolated upstream Apple backends on the same Flutter 3.50.0 binary",
            "otzariaAdoptionApproved": False, "physicalDevice": False})
        trace_error = None
        if args.with_thread_trace:
            try:
                internal_apple_trace.capture(executable=executable, app=app, output=output, documents=documents,
                    manifest=args.corpus_manifest, source_commit=source,
                    provenance_sha256=digest(output / "provenance.json"), run=run, validate=validate,
                    digest=digest, require=require, write_json=write_json)
            except Exception as error:
                trace_error = error
                write_json(output / "trace-failure.json", {"traceComplete": False, "error": traceback.format_exc(),
                    "baselineComplete": baseline_error is None, "timingsComparableWithBaseline": False})
            require({str(p.relative_to(bundle)): digest(p) for p in bundle.rglob("*")
                     if p.is_file() and not p.is_symlink()} == bundle_hashes,
                    "Apple bundle changed during the separate profiler runs")
        require(not run(["git", "status", "--porcelain", "--untracked-files=all"], repo,
                        output / "source-status-after.log"), "Fork source changed during Apple measurement")
        if baseline_error is not None:
            raise RuntimeError("ABBA workload timed out; retained partial evidence and a separate trace diagnostic") from baseline_error
        if trace_error is not None:
            raise RuntimeError("Separate native thread diagnostic failed; see trace-failure.json") from trace_error
    except Exception:
        write_json(output / "failure.json", {"measurementComplete": False, "error": traceback.format_exc()})
        raise
    finally:
        if (output / "upstream").exists():
            shutil.rmtree(output / "upstream")


if __name__ == "__main__":
    main()

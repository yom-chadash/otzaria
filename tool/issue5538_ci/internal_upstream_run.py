"""Run locked upstream suites and actual R01/R10/S15 probes in fork CI."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import traceback


UPSTREAM_SHA = "683df93e986af19bbcefd22606a417083116847b"
FLUTTER_SHA = "ab2912035e160d9cad6306df307576da6128ebdb"
ENGINE_SHA = "371f77202a73e8dff57a8b3bff75bca55c090aee"


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def run(command, cwd, log, timeout=900, extra_env=None):
    result = subprocess.run([str(x) for x in command], cwd=cwd,
        env={**os.environ, "CI": "true", **(extra_env or {})}, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    log.write_text(result.stdout)
    require(result.returncode == 0, f"Command exited {result.returncode}: {command}\n{result.stdout[-5000:]}")
    return result.stdout


def json_test_receipt(text):
    events = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    done = [e for e in events if e.get("type") == "done"]
    tests = [e for e in events if e.get("type") == "testDone"]
    require(done and done[-1].get("success") is True and tests, "Suite did not report successful executed tests")
    receipt = {"executed": sum(not e.get("hidden", False) and not e.get("skipped", False) for e in tests),
               "skipped": sum(e.get("skipped", False) for e in tests), "events": len(events)}
    require(receipt["executed"] > 0, "Suite reported no executed tests")
    return receipt


def node_test_receipt(text):
    totals = {name: int(value) for name, value in re.findall(r"(?m)^# (tests|pass|fail|skipped) (\d+)\s*$", text)}
    require(set(totals) == {"tests", "pass", "fail", "skipped"} and totals["tests"] > 0
            and totals["pass"] > 0 and totals["fail"] == 0, "Node suite did not report executed successful tests")
    return totals


def replace_once(text, old, new):
    require(text.count(old) == 1, "Upstream fault-injection anchor changed: " + old[:100])
    return text.replace(old, new, 1)


SAVE_STATE = """class Issue5538SaveFault {
  static int failAfterBytes = -1;
  static int written = 0;
  static int rejectedWrites = 0;
  static int callbacksClosed = 0;
  static int? nativeResult;
  static void reset(int after) {
    failAfterBytes = after; written = 0; rejectedWrites = 0; callbacksClosed = 0; nativeResult = null;
  }
  static Map<String, Object?> snapshot() => {
    'failAfterBytes': failAfterBytes, 'bytesAccepted': written, 'rejectedWrites': rejectedWrites,
    'callbacksClosed': callbacksClosed, 'nativeSaveResult': nativeResult,
  };
}
"""


def patch_save(text):
    text = replace_once(text, "import 'worker.dart';", "import 'worker.dart';\nimport '../issue5538_save_fault.dart';")
    text = replace_once(text,
        "        byteBuffer.add(Pointer<Uint8>.fromAddress(pData.address).asTypedList(size));\n        return size;",
        """        if (Issue5538SaveFault.failAfterBytes >= 0 && Issue5538SaveFault.written >= Issue5538SaveFault.failAfterBytes) {
          Issue5538SaveFault.rejectedWrites++;
          return 0;
        }
        byteBuffer.add(Pointer<Uint8>.fromAddress(pData.address).asTypedList(size));
        Issue5538SaveFault.written += size;
        return size;""")
    text = replace_once(text, "        pdfium.FPDF_SaveAsCopy(document, fw, flags);",
                        "        Issue5538SaveFault.nativeResult = pdfium.FPDF_SaveAsCopy(document, fw, flags);")
    text = replace_once(text, "        nativeWriteCallable.close();", "        nativeWriteCallable.close();\n        Issue5538SaveFault.callbacksClosed++;")
    return text


def save_probe(module, upstream, output, dart, pdf):
    package = upstream / "packages/pdfrx_engine"
    tool = package / "tool"
    tool.mkdir(exist_ok=True)
    entry = tool / "issue5538_save_probe.dart"
    access = tool / "issue5538_save_access.dart"
    shutil.copyfile(module / "internal_upstream_save.dart.template", entry)
    access.write_text("Future<void> configureFault(int after) async {}\nFuture<Map<String,Object?>> readFault() async => {'instrumented': false};\n")
    run([dart, "run", entry, pdf, output / "native-save-control.json", "control"], package, output / "native-save-control.log")
    native = package / "lib/src/native/pdfrx_pdfium.dart"
    before = native.read_text()
    after = patch_save(before)
    state = package / "lib/src/issue5538_save_fault.dart"
    try:
        native.write_text(after)
        state.write_text(SAVE_STATE)
        access.write_text("""import 'package:pdfrx_engine/src/native/worker.dart';
import 'package:pdfrx_engine/src/issue5538_save_fault.dart';
Future<void> configureFault(int after) => BackgroundWorker.compute((int limit) => Issue5538SaveFault.reset(limit), after);
Future<Map<String,Object?>> readFault() => BackgroundWorker.compute((_) => Issue5538SaveFault.snapshot(), null);
""")
        (output / "save-fault.patch").write_text("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                    fromfile="packages/pdfrx_engine/lib/src/native/pdfrx_pdfium.dart", tofile="packages/pdfrx_engine/lib/src/native/pdfrx_pdfium.dart")))
        write_json(output / "save-fault-provenance.json", {"unmodifiedSourceSha256": hashlib.sha256(before.encode()).hexdigest(),
            "injectedSourceSha256": digest(native), "patchSha256": digest(output / "save-fault.patch"),
            "stateSha256": digest(state), "accessSha256": digest(access), "probeSha256": digest(entry),
            "fault": "Writer returns zero before all bytes are accepted; FPDF_SaveAsCopy remains the real native call.",
            "productFixApplied": False})
        run([dart, "analyze", entry], package, output / "save-fault-analyze.log")
        run([dart, "analyze", access], package, output / "save-fault-access-analyze.log")
        run([dart, "analyze", state], package, output / "save-fault-state-analyze.log")
        run([dart, "run", entry, pdf, output / "native-save-injected.json", "injected"], package, output / "native-save-injected.log")
        control = json.loads((output / "native-save-control.json").read_text())
        injected = json.loads((output / "native-save-injected.json").read_text())
        require(control.get("measurementComplete") is True and len(control["observations"]) == 3, "Missing unmodified native save control")
        require(injected.get("measurementComplete") is True and len(injected["observations"]) == 9, "Missing native failure observations")
        faults = [r for r in injected["observations"] if r["faultAfterBytes"] >= 0]
        require(len(faults) == 6 and all(r["nativeFaultObservation"]["nativeSaveResult"] == 0
            and r["nativeFaultObservation"]["rejectedWrites"] >= 1
            and r["nativeFaultObservation"]["callbacksClosed"] == 1 for r in faults), "Native write failure was not exercised or released")
        return {"controlCases": 3, "injectedFailureCases": 6,
            "failedNativeSavesReturnedNormally": sum(r["apiReturnedNormally"] is True for r in faults)}
    finally:
        native.write_text(before)
        state.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flutter", type=Path, required=True)
    parser.add_argument("--chrome", type=Path, required=True)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--canary", action="store_true")
    args = parser.parse_args()
    require(os.environ.get("CI") == "true", "CI=true required")
    module = Path(__file__).resolve().parent
    root = module.parents[1]
    output = args.output.resolve()
    require(not output.exists(), "Upstream output must be new")
    output.mkdir(parents=True)
    try:
        sdk = json.loads(run([args.flutter, "--version", "--machine"], root, output / "flutter.json"))
        if not args.canary:
            require(sdk.get("frameworkVersion") == "3.50.0" and sdk.get("frameworkRevision") == FLUTTER_SHA
                and sdk.get("engineRevision") == ENGINE_SHA, "Fixed upstream baseline SDK changed")
        source = run(["git", "rev-parse", "HEAD"], root, output / "fork-sha.log").strip()
        dirty = run(["git", "status", "--porcelain", "--untracked-files=all"], root, output / "fork-status.log")
        require(not dirty, "Fork source must be clean")
        upstream = output / "upstream"
        upstream.mkdir()
        run(["git", "init"], upstream, output / "git-init.log")
        run(["git", "remote", "add", "origin", "https://github.com/espresso3389/pdfrx.git"], upstream, output / "git-remote.log")
        run(["git", "fetch", "--depth", "1", "origin", UPSTREAM_SHA], upstream, output / "git-fetch.log")
        run(["git", "checkout", "--detach", "FETCH_HEAD"], upstream, output / "git-checkout.log")
        require(run(["git", "rev-parse", "HEAD"], upstream, output / "upstream-sha.log").strip() == UPSTREAM_SHA, "Upstream SHA mismatch")
        run([args.flutter, "pub", "get"], upstream, output / "pub-get.log")
        lock = upstream / "pubspec.lock"
        shutil.copyfile(lock, output / "upstream-pubspec.lock")
        run([args.flutter, "pub", "get", "--enforce-lockfile"], upstream, output / "pub-get-enforced.log")
        dart = args.flutter.resolve().parent / "cache/dart-sdk/bin/dart"
        engine = upstream / "packages/pdfrx_engine"
        viewer = upstream / "packages/pdfrx"
        node_tests = sorted(viewer.glob("test/*_test.cjs"))
        require(len(node_tests) >= 10, "Expected Node regression suites disappeared")
        node_log = run(["node", "--test", "--test-reporter=tap", *node_tests], viewer, output / "node-suites.log")
        worker_tests = [engine / "test" / name for name in (
            "background_worker_test.dart", "background_worker_task_test.dart", "pdf_render_priority_test.dart")]
        require(all(p.is_file() for p in worker_tests), "Missing native worker priority/task suite")
        engine_log = run([dart, "test", "--concurrency=1", "--reporter=json", *worker_tests], engine, output / "native-suites.jsonl")
        chrome_tests = sorted(viewer.glob("test/wasm_*_test.dart"))
        require(len(chrome_tests) >= 7, "Expected browser regression suites disappeared")
        chrome_log = run([args.flutter, "test", "--platform", "chrome", "--concurrency=1", "--reporter=json", *chrome_tests],
            viewer, output / "chrome-suites.jsonl", extra_env={"CHROME_EXECUTABLE": str(args.chrome)})
        suites = {"native": json_test_receipt(engine_log), "chrome": json_test_receipt(chrome_log),
                  "node": node_test_receipt(node_log), "nodeFiles": len(node_tests)}
        write_json(output / "suite-receipts.json", suites)
        corpus = json.loads(args.corpus_manifest.read_text())
        by_id = {d["id"]: d for d in corpus["documents"]}
        require(len(by_id) == len(corpus["documents"]), "Duplicate corpus document identity")
        paths = {}
        corpus_root = args.corpus_manifest.resolve().parent
        for name in ("mikraot-gedolot-genesis-1859", "gcc-15_2"):
            document = by_id[name]
            relative = Path(document["path"])
            file = (corpus_root / relative).resolve()
            require(not relative.is_absolute() and file.is_relative_to(corpus_root)
                    and file.suffix.lower() == ".pdf" and file.is_file(), "Invalid upstream corpus path")
            require(file.stat().st_size == document["sizeBytes"] and digest(file) == document["sha256"], "Upstream corpus bytes changed")
            paths[name] = file
        run(["node", module / "internal_upstream_node.cjs", "--repo", upstream, "--corpus", paths["mikraot-gedolot-genesis-1859"],
            "--output", output / "web-probes", "--chrome", args.chrome], root, output / "web-probes.log")
        native = save_probe(module, upstream, output, dart, paths["gcc-15_2"])
        run(["git", "diff", "--exit-code"], upstream, output / "source-after.log")
        native_assets = {str(p.relative_to(upstream)): {"sha256": digest(p), "bytes": p.stat().st_size}
                        for p in (upstream / ".dart_tool").rglob("*") if p.is_file() and p.name in ("libpdfium.so", "libpdfium.dylib", "libpdfium_dart.dylib", "pdfium.dll")}
        require(native_assets, "Missing identity receipt for native PDFium used by build hooks")
        write_json(output / "provenance.json", {"upstreamCommit": UPSTREAM_SHA, "forkCommit": source, "forkDirty": False,
            "flutter": sdk, "canary": args.canary, "upstreamLockSha256": digest(lock), "nativeAssets": native_assets,
            "corpusManifestSha256": digest(args.corpus_manifest),
            "harnessFiles": {p.name: digest(p) for p in module.glob("internal_upstream_*") if p.is_file()},
            "nodeTests": [str(p.relative_to(upstream)) for p in node_tests],
            "nativeTests": [str(p.relative_to(upstream)) for p in worker_tests],
            "chromeTests": [str(p.relative_to(upstream)) for p in chrome_tests],
            "appSdkChanged": False, "appAdoptionProven": False,
            "suiteScope": "Native priority/task suites and existing Node/Chrome WASM regressions; not the entire upstream suite",
            "canarySdkIsRecordedAndNotUsedForProductTimingComparison": True})
        write_json(output / "summary.json", {"measurementComplete": True, "suites": suites, "nativeSave": native,
            "sectionsObserved": ["R01", "R10", "S15", "Q01"], "canary": args.canary,
            "knownUnfixedBehaviorCanBeObservedWithoutCallingItFixed": True})
    except Exception:
        write_json(output / "failure.json", {"measurementComplete": False, "error": traceback.format_exc()})
        raise
    finally:
        if (output / "upstream").exists(): shutil.rmtree(output / "upstream")


if __name__ == "__main__":
    main()

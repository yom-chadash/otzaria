"""Fork-only AOT observations of the locked scheduler, HTTP cache and fonts."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import traceback
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import urlopen


ENGINE_ARCHIVE_SHA256 = "001090d220b2773747ce396f2bcda3d6ac9389d87f937ef12d64ed3a9e80e8b0"
PDFIUM_SHA256 = "1c251efce460d67437a4c76d4be9033ae2f17bf5603f5bff1af10209b94ca723"
SDK_REVISION = "abaf9c523780a608bd46686fd5e53740a07077f8"
ENGINE_REVISION = "deb287481e3ce9468f3434937ced4240a70539ca"
MODULES = (
    "internal_run.py", "internal_probe.dart", "internal_http.dart",
    "internal_fonts.dart", "internal_metrics.dart", "internal_metrics_access.dart",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run(command, cwd, log, timeout=600):
    result = subprocess.run([str(x) for x in command], cwd=cwd, env={**os.environ, "CI": "true"},
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    log.write_text(result.stdout, encoding="utf-8")
    require(result.returncode == 0, f"Command exited {result.returncode}: {command}\n{result.stdout[-5000:]}")
    return result.stdout


def replace_once(text, old, new):
    require(text.count(old) == 1, f"Locked source anchor changed: {old[:100]!r}")
    return text.replace(old, new, 1)


def instrument_worker(text):
    text = replace_once(text, "import '../pdfrx.dart';", "import '../pdfrx.dart';\nimport '../issue5538_metrics.dart';")
    text = replace_once(text, "    _queues[params.priority.index].add(params);\n    _scheduleDrain();", """    _queues[params.priority.index].add(params);
    Issue5538Metrics.enqueue(params, _queues.fold<int>(0, (n, q) => n + q.length));
    _scheduleDrain();""")
    text = replace_once(text, """    final params = _next();
    if (!params.execute()) _queues[params.priority.index].addLast(params);""", """    final params = _next();
    final started = Issue5538Metrics.clock.elapsedMicroseconds;
    try {
      if (!params.execute()) {
        _queues[params.priority.index].addLast(params);
        Issue5538Metrics.enqueue(params, _queues.fold<int>(0, (n, q) => n + q.length), continuation: true);
      }
    } finally {
      Issue5538Metrics.count('executeWallUsInstrumented', Issue5538Metrics.clock.elapsedMicroseconds - started);
    }""")
    text = replace_once(text, """  _ComputeParams _next() {
    for (final canceledOnly in [true, false]) {""", """  _ComputeParams _next() {
    final started = Issue5538Metrics.clock.elapsedMicroseconds;
    var canceledProbes = 0;
    var readyProbes = 0;
    for (final canceledOnly in [true, false]) {""")
    text = replace_once(text, """        for (final item in queue) {
          if (canceledOnly ? item.isCanceled : item.isReady) {
            queue.remove(item);
            return item;""", """        for (final item in queue) {
          if (canceledOnly) { canceledProbes++; } else { readyProbes++; }
          if (canceledOnly ? item.isCanceled : item.isReady) {
            queue.remove(item);
            Issue5538Metrics.selected(item, started, canceledProbes, readyProbes);
            return item;""")
    return text


def instrument_native(text):
    text = replace_once(text, "import 'worker.dart';", "import 'worker.dart';\nimport '../issue5538_metrics.dart';")
    for signature, counter in (
        ("  Future<void> configureFontEnvironment({String? fontCachePath, List<String> fontPaths = const []}) async {", "fontConfigure"),
        ("  Future<void> reloadFonts() async {", "fontReload"),
        ("  Future<void> addFontData({required String face, required Uint8List data, String? resolvedFace}) async {", "fontRegisterData"),
        ("  Future<void> addFontFile({required String face, required String filePath, String? resolvedFace}) async {", "fontRegisterFile"),
        ("  Future<PdfPageRawText?> loadText() async {", "rawTextCalls"),
    ):
        text = replace_once(text, signature, signature + f"\n    Issue5538Metrics.count('{counter}');")
    return text


def read_packages(config):
    value = json.loads(config.read_text())
    for package in value["packages"]:
        package["rootUri"] = urljoin(config.as_uri(), package["rootUri"])
    return value


def package_root(packages, name):
    entry = next(item for item in packages["packages"] if item["name"] == name)
    uri = urlparse(entry["rootUri"])
    require(uri.scheme == "file", f"Unexpected package URI: {uri.scheme}")
    return Path(unquote(uri.path))


def verify_engine_archive(output, resolved, supplied=None):
    archive = output / "pdfrx_engine-0.9.3.tar.gz"
    if supplied:
        shutil.copyfile(supplied, archive)
    else:
        with urlopen("https://pub.dev/api/archives/pdfrx_engine-0.9.3.tar.gz", timeout=120) as response, archive.open("wb") as target:
            shutil.copyfileobj(response, target)
    require(digest(archive) == ENGINE_ARCHIVE_SHA256, "Engine archive differs from pubspec.lock")
    extracted = output / "engine-original"
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(extracted, filter="data")
    original_files = {p.relative_to(extracted).as_posix(): digest(p) for p in extracted.rglob("*") if p.is_file()}
    for relative, expected in original_files.items():
        if relative.startswith("lib/") or relative in ("pubspec.yaml", "LICENSE"):
            require(digest(resolved / relative) == expected, f"Resolved engine differs from signed-off archive: {relative}")
    return extracted, original_files


def prepare_variant(output, module_dir, packages, original, instrumented):
    variant = output / ("instrumented" if instrumented else "control")
    variant.mkdir()
    for name in MODULES:
        if name.endswith(".dart"):
            shutil.copyfile(module_dir / name, variant / name)
    config = json.loads(json.dumps(packages))
    patches = {}
    if instrumented:
        copied = variant / "engine"
        shutil.copytree(original / "lib", copied / "lib")
        for name in ("LICENSE", "pubspec.yaml"):
            shutil.copyfile(original / name, copied / name)
        (copied / "lib/src/issue5538_metrics.dart").write_bytes((module_dir / "internal_metrics.dart").read_bytes())
        for relative, patch in (("lib/src/native/worker.dart", instrument_worker), ("lib/src/native/pdfrx_pdfium.dart", instrument_native)):
            file = copied / relative
            before = file.read_text()
            after = patch(before)
            file.write_text(after)
            patches[relative] = {"beforeSha256": hashlib.sha256(before.encode()).hexdigest(), "afterSha256": digest(file)}
            (variant / (file.stem + ".patch")).write_text("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile=relative, tofile=relative)))
        next(p for p in config["packages"] if p["name"] == "pdfrx_engine")["rootUri"] = copied.as_uri() + "/"
        (variant / "internal_metrics_access.dart").write_text("""import 'package:pdfrx_engine/src/native/worker.dart';
import 'package:pdfrx_engine/src/issue5538_metrics.dart';
Future<Map<String, Object?>> takeInternalMetrics({bool reset = false}) async {
  final worker = await BackgroundWorker.compute((bool reset) => Issue5538Metrics.snapshot(reset: reset), reset);
  return {'instrumented': true, 'worker': worker, 'caller': Issue5538Metrics.snapshot(reset: reset),
    'measurementCommandsIncluded': 1, 'instrumentedTimesAreProductTimes': false};
}
""")
    else:
        next(p for p in config["packages"] if p["name"] == "pdfrx_engine")["rootUri"] = original.as_uri() + "/"
    write_json(variant / "packages.json", config)
    write_json(variant / "patches.json", patches)
    return variant


def dependency_receipt(path):
    text = path.read_text().replace("\\\n", "")
    require(": " in text, "Missing compiler dependency list")
    paths = {Path(p).resolve() for p in shlex.split(text.split(": ", 1)[1])}
    return {str(p): digest(p) for p in sorted(paths)}


def validate_report(report, instrumented):
    require(report.get("measurementComplete") is True and report.get("executionMode") == "dart-aot", "Incomplete AOT observation")
    require(report.get("instrumented") is instrumented and report.get("uiLatencyCollected") is False, "Wrong observation scope")
    rows = report.get("observations", [])
    require(rows and all(row.get("measurementComplete") is True for row in rows), "A scenario did not finish")
    require({row["section"] for row in rows} == {"M01", "E10", "S02"}, "Missing assigned measurement section")
    require(any(r.get("workload") == "native-pdf-mixed" for r in rows), "No real native PDF scheduler workload")
    require(any(r.get("workload") == "overlapping-cache-reads" for r in rows), "No overlapping HTTP observation")
    require(report.get("cleanupErrors") == [], "Native ownership did not close cleanly")
    if instrumented:
        sched = [r for r in rows if r.get("workload") == "worker-burst"]
        require(sched and all(r["metrics"]["worker"]["counters"].get("selections", 0) >= r["tasks"] for r in sched), "Missing scheduler counters")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dart", type=Path, required=True)
    parser.add_argument("--packages", type=Path)
    parser.add_argument("--pdfium", type=Path, required=True)
    parser.add_argument("--flutter-json", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, action="append", required=True)
    parser.add_argument("--http-pdf", type=Path, required=True)
    parser.add_argument("--http-linearized", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine-archive", type=Path)
    parser.add_argument("--allow-uncommitted-validation", action="store_true")
    args = parser.parse_args()
    require(os.environ.get("CI") == "true", "CI=true is required")
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    require(not output.exists(), "Output must be new")
    output.mkdir(parents=True)
    try:
        require(platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64"), "This producer is pinned to Linux x64")
        sdk = json.loads(args.flutter_json.read_text())
        require(sdk.get("frameworkRevision") == SDK_REVISION and sdk.get("engineRevision") == ENGINE_REVISION, "Flutter/engine pin drift")
        version = run([args.dart, "--version"], root, output / "dart-version.log")
        require("Dart SDK version: 3.13.5 " in version, "Dart version drift")
        require(digest(args.pdfium) == PDFIUM_SHA256, "PDFium producer identity drift")
        lock = (root / "pubspec.lock").read_text()
        section = re.search(r"(?ms)^  pdfrx_engine:\n(.*?)(?=^  [a-zA-Z_]+:|\Z)", lock)
        require(section and ENGINE_ARCHIVE_SHA256 in section.group(1) and 'version: "0.9.3"' in section.group(1), "Locked engine changed")
        commit = run(["git", "rev-parse", "HEAD"], root, output / "git-head.log").strip()
        dirty = run(["git", "status", "--porcelain", "--untracked-files=all"], root, output / "git-status.log")
        require(not dirty or args.allow_uncommitted_validation, "Measurement source must be committed and clean")
        config_path = (args.packages or root / ".dart_tool/package_config.json").resolve()
        packages = read_packages(config_path)
        original, hashes = verify_engine_archive(output, package_root(packages, "pdfrx_engine"), args.engine_archive)
        module_dir = Path(__file__).resolve().parent
        source = {str(p.relative_to(root)): digest(p) for p in [root / "pubspec.yaml", root / "pubspec.lock", *(module_dir / n for n in MODULES)]}
        inputs = {str(p.resolve()): {"sha256": digest(p), "bytes": p.stat().st_size} for p in [*args.pdf, args.http_pdf, args.http_linearized, args.font, args.pdfium]}
        write_json(output / "provenance.json", {"commit": commit, "dirty": bool(dirty), "comparativeEvidence": not dirty,
            "source": source, "sdk": sdk, "dartSha256": digest(args.dart), "pdfiumSha256": PDFIUM_SHA256,
            "packageConfigSha256": digest(config_path), "engineArchiveSha256": ENGINE_ARCHIVE_SHA256,
            "engineSource": "https://pub.dev/api/archives/pdfrx_engine-0.9.3.tar.gz", "archiveFiles": hashes,
            "inputFiles": inputs, "runOrder": ["control", "instrumented", "instrumented", "control"],
            "timingClaim": "Control timings only; instrumented timings include measurement overhead.",
            "osCacheCleared": False, "physicalDevice": False})
        variants = {}
        for instrumented in (False, True):
            variant = prepare_variant(output, module_dir, packages, original, instrumented)
            command = [args.dart, "compile", "exe", f"--packages={variant / 'packages.json'}", "-DINTERNAL_MODE=dart-aot",
                       f"-DINTERNAL_INSTRUMENTED={'true' if instrumented else 'false'}", f"--depfile={variant / 'compile.d'}",
                       variant / "internal_probe.dart", "-o", variant / "probe"]
            write_json(variant / "compile-command.json", [str(x) for x in command])
            run(command, root, variant / "compile.log")
            write_json(variant / "compiler-inputs.json", dependency_receipt(variant / "compile.d"))
            variants[instrumented] = variant
        runs = []
        for index, instrumented in enumerate((False, True, True, False)):
            variant = variants[instrumented]
            destination = output / f"run-{index + 1}-{variant.name}"
            command = [variant / "probe", "--output", destination, "--pdfium", args.pdfium.resolve(), "--font", args.font.resolve(),
                       "--http-pdf", args.http_pdf.resolve(), "--http-linearized", args.http_linearized.resolve()]
            for pdf in args.pdf:
                command += ["--pdf", pdf.resolve()]
            run(command, root, output / f"run-{index + 1}.log", timeout=1500)
            report = json.loads((destination / "report.json").read_text())
            validate_report(report, instrumented)
            cache_files = [p for p in (destination / "cache").rglob("*") if p.is_file()]
            runs.append({"index": index + 1, "variant": variant.name, "executableSha256": digest(variant / "probe"),
                         "reportSha256": digest(destination / "report.json"), "report": str(destination / "report.json"),
                         "cacheFiles": len(cache_files), "cacheLogicalBytes": sum(p.stat().st_size for p in cache_files),
                         "cacheAllocatedBytes": sum(p.stat().st_blocks * 512 for p in cache_files)})
            write_json(destination / "cache-files.json", [
                {"path": str(p.relative_to(destination)), "logicalBytes": p.stat().st_size,
                 "allocatedBytes": p.stat().st_blocks * 512} for p in cache_files
            ])
            shutil.rmtree(destination / "cache")
        require(all(digest(Path(name)) == info["sha256"] for name, info in inputs.items()), "Measured input changed")
        require(all(digest(root / name) == value for name, value in source.items()), "Harness source changed")
        write_json(output / "summary.json", {"measurementComplete": True, "runs": runs, "productImprovementClaim": False,
                    "sectionsMeasured": ["M01 scheduler", "E10 HTTP cache", "S02 fonts"], "comparativeEvidence": not dirty})
    except Exception:
        write_json(output / "failure.json", {"measurementComplete": False, "error": traceback.format_exc()})
        raise


if __name__ == "__main__":
    main()

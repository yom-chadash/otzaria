"""Fork-only E03 decomposition of the locked native text worker."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import traceback

from internal_run import (
    ENGINE_ARCHIVE_SHA256, ENGINE_REVISION, PDFIUM_SHA256, SDK_REVISION,
    dependency_receipt, digest, package_root, read_packages, replace_once,
    require, run, verify_engine_archive, write_json,
)


MODULES = (
    "internal_run.py", "internal_text_run.py", "internal_text_probe.dart",
    "internal_text_metrics.dart", "internal_text_access.dart", "corpus_sources.json",
)
REFERENCE_PAGES = {"uxlc-genesis": 1, "mikraot-gedolot-genesis-1859": 10}
PROOF_FIELDS = ("hasRawText", "utf16CodeUnits", "characterRects", "textSha256", "geometrySha256")
WORKER_FIELDS = ("workerRawBuildUs", "workerNormalizationUs", "workerCleanupUs", "workerBodyUs")
HEBREW_MARKS = "[\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7]"


def instrument_text(text):
    signature = "  Future<PdfPageRawText?> loadText() async {"
    require(text.count(signature) == 1, "Locked loadText signature changed")
    start = text.index(signature)
    end = text.index("\n  @override", start)
    method = text[start:end]
    method = replace_once(method, """      (arena, params) {
        final doubleSize = sizeOf<Double>();""", """      (arena, params) {
        final issue5538Clock = Stopwatch()..start();
        var issue5538RawBuildUs = 0;
        var issue5538NormalizationUs = 0;
        var issue5538Completed = false;
        final doubleSize = sizeOf<Double>();""")
    method = replace_once(method, """          final raw = PdfPageRawText(sb.toString(), charRects);
          // On the worker, so the reordering does not cost the UI isolate.
          return params.normalizeHebrew
              ? PdfHebrewTextNormalizer.normalize(raw, decodeLegacyEncoding: params.decodeLegacyHebrew)
              : raw;""", """          final raw = PdfPageRawText(sb.toString(), charRects);
          issue5538RawBuildUs = issue5538Clock.elapsedMicroseconds;
          final issue5538NormalizeStart = issue5538Clock.elapsedMicroseconds;
          final result = params.normalizeHebrew
              ? PdfHebrewTextNormalizer.normalize(raw, decodeLegacyEncoding: params.decodeLegacyHebrew)
              : raw;
          issue5538NormalizationUs = issue5538Clock.elapsedMicroseconds - issue5538NormalizeStart;
          issue5538Completed = true;
          return result;""")
    method = replace_once(method, """        } finally {
          pdfium.FPDFText_ClosePage(textPage);
          pdfium.FPDF_ClosePage(page);
        }""", """        } finally {
          final issue5538CleanupStart = issue5538Clock.elapsedMicroseconds;
          pdfium.FPDFText_ClosePage(textPage);
          pdfium.FPDF_ClosePage(page);
          final issue5538CleanupUs = issue5538Clock.elapsedMicroseconds - issue5538CleanupStart;
          Issue5538TextMetrics.record(
            pageNumber: params.pageNumber,
            rawBuildUs: issue5538RawBuildUs,
            normalizationUs: issue5538NormalizationUs,
            cleanupUs: issue5538CleanupUs,
            workerBodyUs: issue5538Clock.elapsedMicroseconds,
            completed: issue5538Completed,
            normalizeHebrew: params.normalizeHebrew,
          );
        }""")
    result = text[:start] + method + text[end:]
    return replace_once(result, "import 'worker.dart';", "import 'worker.dart';\nimport '../issue5538_text_metrics.dart';")


def prepare_variant(output, modules, packages, original, instrumented):
    variant = output / ("instrumented" if instrumented else "control")
    variant.mkdir()
    for name in MODULES:
        if name.endswith(".dart"):
            shutil.copyfile(modules / name, variant / name)
    config = json.loads(json.dumps(packages))
    engine = original
    patch_receipt = {}
    if instrumented:
        engine = variant / "engine"
        shutil.copytree(original / "lib", engine / "lib")
        for name in ("LICENSE", "pubspec.yaml"):
            shutil.copyfile(original / name, engine / name)
        metrics = engine / "lib/src/issue5538_text_metrics.dart"
        shutil.copyfile(modules / "internal_text_metrics.dart", metrics)
        target = engine / "lib/src/native/pdfrx_pdfium.dart"
        before = target.read_text(encoding="utf-8")
        after = instrument_text(before)
        target.write_text(after, encoding="utf-8")
        patch_receipt = {
            "target": "lib/src/native/pdfrx_pdfium.dart",
            "beforeSha256": hashlib.sha256(before.encode()).hexdigest(),
            "afterSha256": digest(target), "metricsSha256": digest(metrics),
            "returnedPdfPageRawTextContractChanged": False,
        }
        (variant / "instrumentation.patch").write_text("".join(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True),
            fromfile=patch_receipt["target"], tofile=patch_receipt["target"],
        )), encoding="utf-8")
        (variant / "internal_text_access.dart").write_text("""import 'package:pdfrx_engine/src/native/worker.dart';
import 'package:pdfrx_engine/src/issue5538_text_metrics.dart';
Future<List<Map<String, Object?>>> takeTextMetrics({required bool reset}) =>
    BackgroundWorker.compute((bool reset) => Issue5538TextMetrics.take(reset: reset), reset);
""", encoding="utf-8")
    next(p for p in config["packages"] if p["name"] == "pdfrx_engine")["rootUri"] = engine.as_uri() + "/"
    write_json(variant / "packages.json", config)
    write_json(variant / "patches.json", patch_receipt)
    return variant


def validate_report(report, config, instrumented):
    require(report.get("measurementComplete") is True and report.get("executionMode") == "dart-aot", "Incomplete AOT report")
    require(report.get("instrumented") is instrumented, "Wrong variant")
    for field in ("sourceCommit", "inputSha256", "pdfiumSha256", "pageCount", "warmup", "repetitions"):
        require(report.get(field) == config[field], f"Report identity mismatch: {field}")
    rows = report.get("observations", [])
    expected = {(iteration, page) for iteration in range(config["warmup"] + config["repetitions"])
                for page in range(1, config["pageCount"] + 1)}
    require(len(rows) == len(expected) and {(r["iteration"], r["pageNumber"]) for r in rows} == expected,
            "Page/pass observation grid is incomplete or duplicated")
    proofs = {}
    for row in rows:
        require(row.get("warmup") is (row["iteration"] < config["warmup"]), "Wrong warmup label")
        require(type(row.get("callerRawLoadUs")) is int and row["callerRawLoadUs"] >= 0, "Invalid caller interval")
        proof = tuple(row[field] for field in PROOF_FIELDS)
        require(all(re.fullmatch("[0-9a-f]{64}", row[field]) for field in ("textSha256", "geometrySha256")), "Malformed parity hash")
        require(proofs.setdefault(row["pageNumber"], proof) == proof, "Text/geometry changed between passes")
        if instrumented:
            worker = row.get("worker", {})
            require(worker.get("pageNumber") == row["pageNumber"] and worker.get("completed") is True
                    and worker.get("normalizeHebrew") is True, "Worker identity mismatch")
            require(all(type(worker.get(field)) is int and worker[field] >= 0 for field in WORKER_FIELDS), "Invalid worker interval")
            require(sum(worker[field] for field in WORKER_FIELDS[:3]) <= worker["workerBodyUs"], "Worker intervals overlap")
            residual = row["callerRawLoadUs"] - worker["workerBodyUs"]
            require(residual >= 0 and row.get("outsideMeasuredWorkerBodyUs") == residual, "Invalid outside-worker residual")
        else:
            require("worker" not in row and "outsideMeasuredWorkerBodyUs" not in row, "Control is instrumented")
    reference = report.get("reference", {})
    require(reference.get("pageNumber") == config["referencePage"] and reference.get("timed") is False, "Wrong content reference")
    require(tuple(reference["normalized"][field] for field in PROOF_FIELDS) == proofs[config["referencePage"]], "Reference differs from timed page")
    require(reference["beforeHebrewTransforms"]["utf16CodeUnits"] >= 1000
            and reference["beforeHebrewTransforms"]["hebrewMarks"] > 0, "Reference page lacks real content")
    return proofs


def text_summary(file):
    text = file.read_bytes().decode("utf-8")
    pattern = re.compile((HEBREW_MARKS + "*").join("אלהים"))
    matches = list(pattern.finditer(text))
    return {
        "sha256": digest(file), "utf16CodeUnits": len(text.encode("utf-16-le")) // 2,
        "utf8Bytes": file.stat().st_size, "hebrewLetters": len(re.findall("[א-ת]", text)),
        "hebrewMarks": len(re.findall(HEBREW_MARKS, text)), "literalQuery": "אלהים",
        "literalHits": text.count("אלהים"), "niqqudInsensitiveHits": len(matches),
        "niqqudInsensitiveExamples": [m.group() for m in matches[:3]],
        "firstHebrewContext": next((text[max(0, m.start() - 15):m.start() + 110]
                                    for m in re.finditer("[א-ת]", text)), ""),
        "queryRemovesWhitespace": False,
    }


def reference_comparison(output, pdf, page, runs, root):
    target = output / "reference"
    target.mkdir()
    poppler = shutil.which("pdftotext")
    require(poppler is not None, "Poppler pdftotext is required for the content reference")
    version = run([poppler, "-v"], root, target / "poppler-version.log")
    commands = []
    outputs = {}
    for mode in ("raw", "layout"):
        path = target / f"poppler-{mode}.txt"
        command = [poppler, "-f", str(page), "-l", str(page), f"-{mode}", "-enc", "UTF-8", "-nopgbrk", pdf, path]
        run(command, root, target / f"poppler-{mode}.log")
        commands.append([str(x) for x in command])
        outputs[f"poppler-{mode}"] = text_summary(path)
        require(outputs[f"poppler-{mode}"]["hebrewLetters"] >= 100, "Poppler reference has no Hebrew content")
    for name in ("normalized", "before-normalization"):
        hashes = {digest(path / f"reference-{name}.txt") for path in runs}
        require(len(hashes) == 1, "Reference text differs between control and instrumented")
        path = target / f"pdfium-{name}.txt"
        shutil.copyfile(runs[0] / f"reference-{name}.txt", path)
        outputs[f"pdfium-{name}"] = text_summary(path)
        reference = json.loads((runs[0] / "report.json").read_text())["reference"]
        proof = reference["normalized" if name == "normalized" else "beforeHebrewTransforms"]
        require(outputs[f"pdfium-{name}"]["utf16CodeUnits"] == proof["utf16CodeUnits"]
                and outputs[f"pdfium-{name}"]["sha256"] == proof["textSha256"], "Reference byte/character receipt differs from Dart")
    return {
        "pageNumber": page, "timed": False, "popplerVersion": version.strip(),
        "popplerExecutable": poppler, "popplerExecutableSha256": digest(poppler),
        "commands": commands, "outputs": outputs,
        "normalizationChangedText": outputs["pdfium-normalized"]["sha256"] != outputs["pdfium-before-normalization"]["sha256"],
        "allExtractorsHaveIdenticalBytes": len({row["sha256"] for row in outputs.values()}) == 1,
        "limitations": ["Poppler is an independent extractor, not OCR ground truth.",
                        "PDFium with both Hebrew flags off can still infer spacing; this does not identify PDF object bytes.",
                        "The query retains whitespace and only permits Hebrew marks between letters."],
    }


def timing_summary(reports):
    variants = {}
    for label in ("control", "instrumented"):
        process_summaries = []
        for index, report in enumerate(reports):
            if report["instrumented"] is not (label == "instrumented"):
                continue
            passes = []
            for iteration in range(report["warmup"], report["warmup"] + report["repetitions"]):
                rows = [r for r in report["observations"] if r["iteration"] == iteration]
                sums = {"callerRawLoadUs": sum(r["callerRawLoadUs"] for r in rows)}
                if label == "instrumented":
                    sums.update({field: sum(r["worker"][field] for r in rows) for field in WORKER_FIELDS})
                    sums["outsideMeasuredWorkerBodyUs"] = sum(r["outsideMeasuredWorkerBodyUs"] for r in rows)
                    sums["workerUnattributedUs"] = sums["workerBodyUs"] - sum(sums[field] for field in WORKER_FIELDS[:3])
                passes.append({"iteration": iteration, **sums})
            medians = {field: statistics.median([row[field] for row in passes]) for field in passes[0] if field != "iteration"}
            process_summaries.append({"launch": index + 1, "passes": passes, "passMedianUs": medians})
        variants[label] = {"independentProcesses": len(process_summaries), "processes": process_summaries}
    return variants


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dart", type=Path, required=True)
    parser.add_argument("--packages", type=Path)
    parser.add_argument("--pdfium", type=Path, required=True)
    parser.add_argument("--flutter-json", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--document", choices=tuple(REFERENCE_PAGES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine-archive", type=Path)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--allow-uncommitted-validation", action="store_true")
    args = parser.parse_args()
    require(os.environ.get("CI") == "true", "CI=true is required")
    require(1 <= args.repetitions <= 10, "Repetitions must be between 1 and 10")
    root = Path(__file__).resolve().parents[2]
    modules = Path(__file__).resolve().parent
    output = args.output.resolve()
    require(not output.exists(), "Output must be new")
    require(not output.is_relative_to(root), "Output and vendored sources must be outside the checkout")
    output.mkdir(parents=True)
    try:
        require(platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64"), "Producer is pinned to Linux x64")
        sdk = json.loads(args.flutter_json.read_text())
        require(sdk.get("frameworkRevision") == SDK_REVISION and sdk.get("engineRevision") == ENGINE_REVISION, "Flutter/engine pin drift")
        version = run([args.dart, "--version"], root, output / "dart-version.log")
        require("Dart SDK version: 3.13.5 " in version, "Dart version drift")
        require(digest(args.pdfium) == PDFIUM_SHA256, "PDFium identity drift")
        document = next(row for row in json.loads((modules / "corpus_sources.json").read_text())["documents"] if row["id"] == args.document)
        require(digest(args.pdf) == document["sha256"] and args.pdf.stat().st_size == document["sizeBytes"], "Corpus does not match pinned bytes")
        lock = (root / "pubspec.lock").read_text()
        section = re.search(r"(?ms)^  pdfrx_engine:\n(.*?)(?=^  [a-zA-Z_]+:|\Z)", lock)
        require(section and ENGINE_ARCHIVE_SHA256 in section.group(1) and 'version: "0.9.3"' in section.group(1), "Locked engine changed")
        commit = run(["git", "rev-parse", "HEAD"], root, output / "git-head.log").strip()
        dirty = run(["git", "status", "--porcelain", "--untracked-files=all"], root, output / "git-status.log")
        require(not dirty or args.allow_uncommitted_validation, "Measurement source must be committed and clean")
        config_path = (args.packages or root / ".dart_tool/package_config.json").resolve()
        packages = read_packages(config_path)
        original, archive_files = verify_engine_archive(output, package_root(packages, "pdfrx_engine"), args.engine_archive)
        source = {str(p.relative_to(root)): digest(p) for p in [root / "pubspec.yaml", root / "pubspec.lock", *(modules / name for name in MODULES)]}
        inputs = {str(p.resolve()): digest(p) for p in (args.pdf, args.pdfium, args.flutter_json, args.dart, config_path)}
        write_json(output / "provenance.json", {
            "commit": commit, "dirty": bool(dirty), "comparativeEvidence": not dirty, "source": source,
            "sdk": sdk, "inputFiles": inputs, "packageConfigSha256": digest(config_path),
            "engineArchiveSha256": ENGINE_ARCHIVE_SHA256, "archiveFiles": archive_files,
            "packageResolution": "Private mapping of the officially resolved app configuration; only pdfrx_engine rootUri changes.",
            "runOrder": ["control", "instrumented", "instrumented", "control"],
            "productImprovementClaim": False, "osCacheCleared": False,
        })
        variants = {}
        frozen = {}
        for instrumented in (False, True):
            variant = prepare_variant(output, modules, packages, original, instrumented)
            command = [args.dart, "compile", "exe", f"--packages={variant / 'packages.json'}", "-DTEXT_MODE=dart-aot",
                       f"-DTEXT_INSTRUMENTED={'true' if instrumented else 'false'}", f"-DTEXT_SOURCE_COMMIT={commit}",
                       f"--depfile={variant / 'compile.d'}", variant / "internal_text_probe.dart", "-o", variant / "probe"]
            write_json(variant / "compile-command.json", [str(x) for x in command])
            run(command, root, variant / "compile.log")
            dependencies = dependency_receipt(variant / "compile.d")
            write_json(variant / "compiler-inputs.json", dependencies)
            frozen.update(dependencies)
            frozen[str(variant / "probe")] = digest(variant / "probe")
            frozen[str(variant / "packages.json")] = digest(variant / "packages.json")
            variants[instrumented] = variant
        reports, directories, receipts = [], [], []
        expected_proofs = None
        expected_reference = None
        for index, instrumented in enumerate((False, True, True, False)):
            require(all(digest(path) == expected for path, expected in frozen.items()), "Compiler inputs or executables changed before launch")
            variant = variants[instrumented]
            destination = output / f"run-{index + 1}-{variant.name}"
            config = {"output": str(destination), "input": str(args.pdf.resolve()), "inputSha256": document["sha256"],
                      "pdfium": str(args.pdfium.resolve()), "pdfiumSha256": PDFIUM_SHA256, "sourceCommit": commit,
                      "pageCount": document["pageCount"], "warmup": 1, "repetitions": args.repetitions,
                      "referencePage": REFERENCE_PAGES[args.document]}
            config_file = output / f"run-{index + 1}-config.json"
            write_json(config_file, config)
            run([variant / "probe", config_file], root, output / f"run-{index + 1}.log", timeout=1500)
            report = json.loads((destination / "report.json").read_text())
            proofs = validate_report(report, config, instrumented)
            expected_proofs = proofs if expected_proofs is None else expected_proofs
            require(proofs == expected_proofs, "Control/instrumented text or full geometry differs")
            expected_reference = report["reference"] if expected_reference is None else expected_reference
            require(report["reference"] == expected_reference, "Untimed normalization reference differs between variants")
            raw_rows = [json.loads(line) for line in (destination / "observations.jsonl").read_text().splitlines()]
            require(raw_rows == report["observations"], "Raw observations differ from the report")
            reports.append(report)
            directories.append(destination)
            receipts.append({"launch": index + 1, "variant": variant.name, "reportSha256": digest(destination / "report.json"),
                             "observationsSha256": digest(destination / "observations.jsonl"), "executableSha256": digest(variant / "probe")})
        reference = reference_comparison(output, args.pdf.resolve(), REFERENCE_PAGES[args.document], directories, root)
        write_json(output / "reference-comparison.json", reference)
        require(all(digest(path) == expected for path, expected in frozen.items()), "Compiler inputs or executables changed after measurements")
        require(all(digest(path) == expected for path, expected in inputs.items()), "Input identity changed")
        require(all(digest(root / name) == expected for name, expected in source.items()), "Harness or lock changed")
        final_commit = run(["git", "rev-parse", "HEAD"], root, output / "git-head-after.log").strip()
        final_dirty = run(["git", "status", "--porcelain", "--untracked-files=all"], root, output / "git-status-after.log")
        require(final_commit == commit and final_dirty == dirty, "Checkout identity changed")
        write_json(output / "summary.json", {
            "measurementComplete": True, "section": "E03", "document": document,
            "comparativeEvidence": not dirty, "productImprovementClaim": False,
            "runs": receipts, "textAndFullGeometryParity": True,
            "parityObservations": sum(len(report["observations"]) for report in reports),
            "timing": timing_summary(reports), "referenceComparison": reference,
            "limitations": reports[0]["limitations"] + [
                "Two independent processes per variant; repeated passes are not independent processes.",
                "Hashes are computed between timed loads and can affect later GC or caches in both variants.",
                "Instrumentation adds clocks and metric bookkeeping; the control is an overhead reference, not a product baseline/candidate.",
            ],
        })
    except Exception:
        write_json(output / "failure.json", {"measurementComplete": False, "error": traceback.format_exc()})
        raise


if __name__ == "__main__":
    main()

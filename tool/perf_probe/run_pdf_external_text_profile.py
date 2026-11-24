"""Run the pinned native text stages on a SHA-256 identified external PDF."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sys
import traceback

import run_pdf_text_profile as common


SOURCE_FILES = (
    "pubspec.yaml", "pubspec.lock", "lib/main.dart",
    "tool/perf_probe/run_pdf_external_text_profile.dart",
    "tool/perf_probe/run_pdf_external_text_profile.py",
    "tool/perf_probe/run_pdf_text_profile.py",
    "tool/perf_probe/src/pdf_text_profile.dart",
    "tool/perf_probe/src/pdf_text_profile_report.dart",
    "tool/perf_probe/src/report.dart",
    "tool/perf_probe/src/pdf_image_budget.dart",
)
ENTRY_POINT = "tool/perf_probe/run_pdf_external_text_profile.dart"
QUERY_IDS = ("literal_elohim", "pointed_elohim", "hebrew_combining_marks", "no_match", "zero_length")
PHASES = ("pipeline", "cacheFirst", "cacheRepeat")
TIMINGS = ("rawLoadUs", "formatUs", "cacheLoadUs", "matchingUs", "rectsUs")
require = common.require
digest = common.digest
run = common.run
write_json = common.write_json


def source_receipt(repo):
    dirty = run(["git", "status", "--porcelain", "--untracked-files=all", "--", *SOURCE_FILES], repo)
    require(not dirty, f"Measured source is dirty: {dirty}")
    entries = run(["git", "ls-tree", "-r", "HEAD", "--", *SOURCE_FILES], repo).splitlines()
    blobs = {line.split("\t", 1)[1]: line.split("\t", 1)[0].split()[2] for line in entries}
    require(set(blobs) == set(SOURCE_FILES), "Measured source is missing from HEAD")
    hashes = {}
    for name in SOURCE_FILES:
        path = repo / name
        require(path.is_file() and not path.is_symlink(), f"Not a regular source file: {name}")
        data = path.read_bytes()
        require(hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() == blobs[name], f"Source differs from HEAD: {name}")
        hashes[name] = hashlib.sha256(data).hexdigest()
    header = run(["git", "cat-file", "commit", "HEAD"], repo).split("\n\n", 1)[0]
    return {
        "commit": run(["git", "rev-parse", "HEAD"], repo).strip(),
        "tree": run(["git", "rev-parse", "HEAD^{tree}"], repo).strip(),
        "parents": [line[7:] for line in header.splitlines() if line.startswith("parent ")],
        "files": hashes, "blobs": blobs,
    }


def compiler_dependencies(depfile, repo):
    repo = repo.resolve()
    text = depfile.read_text(encoding="utf-8").replace("\\\n", "")
    require(": " in text, "Missing compiler dependency output")
    paths = {Path(value).resolve() for value in shlex.split(text.split(": ", 1)[1])}
    hashes = {}
    for path in sorted(paths):
        require(path.is_file(), f"Missing compiler input: {path}")
        if path.is_relative_to(repo):
            name = path.relative_to(repo).as_posix()
            require(name in SOURCE_FILES or name == ".dart_tool/package_config.json", f"Unrecorded compiler input: {name}")
        hashes[str(path)] = digest(path)
    require(str(repo / ENTRY_POINT) in hashes, "Compiler did not record the measured entry point")
    return hashes


def distribution(values):
    if not values:
        return None
    values = sorted(values)
    return {
        "count": len(values), "min": values[0], "mean": sum(values) / len(values),
        "p50": values[math.ceil(.50 * len(values)) - 1],
        "p95": values[math.ceil(.95 * len(values)) - 1],
        "p99": values[math.ceil(.99 * len(values)) - 1], "max": values[-1],
    }


def independent_extraction(input_pdf, output, repo):
    binary = shutil.which("pdftotext")
    require(binary is not None, "Install poppler-utils to preserve an independent extraction reference")
    binary = Path(binary).resolve(strict=True)
    before = digest(binary)
    version = run([binary, "-v"], repo).splitlines()[0]
    references = {}
    for mode in ("raw", "layout"):
        target = output / f"pdftotext-page-1-{mode}.txt"
        command = [binary, f"-{mode}", "-f", "1", "-l", "1", input_pdf, target]
        run(command, repo, log=output / f"pdftotext-{mode}.log")
        content = target.read_text(encoding="utf-8")
        references[mode] = {"file": target.name, "sha256": digest(target), "characters": len(content), "excerpt": content[:350], "command": [str(value) for value in command]}
    require(digest(binary) == before, "Independent extractor changed during execution")
    result = {
        "status": "passed", "binarySha256": before, "version": version,
        "scope": "Independent Poppler extraction of page 1; no whitespace or bidi control removal",
        "sourcePdfEncodedSpaceClaims": False, "references": references,
    }
    write_json(output / "independent-extraction.json", result)
    return result


def validate_native_report(report, output, *, input_hash, label, warmup, repetitions):
    require(report.get("protocolVersion") == 1 and report.get("status") == "passed", "Incomplete native report")
    require(report.get("component") == "pdfrx-public-text-pipeline-external-corpus" and report.get("executionMode") == "dart-aot", "Not the requested external AOT run")
    require(report.get("pdfiumSha256") == common.PDFIUM_SHA256 and report.get("dartVersion", "").startswith("3.13.5 "), "Runtime identity differs")
    for name in ("uiLatencyCollected", "nativeTransferSeparated", "osCacheCleared"):
        require(report.get(name) is False, f"Unsupported measurement claim: {name}")
    corpus = report["corpus"]
    count = corpus["pageCount"]
    require(type(count) is int and count > 16, "External corpus must exceed the LRU")
    require(corpus["kind"] == "external-pdf" and corpus["label"] == label and corpus["sha256"] == input_hash, "External corpus identity differs")
    require(corpus["whitespaceModified"] is False and corpus["textAccuracyGroundTruthAvailable"] is False, "Unsupported text correctness claim")
    require(digest(output / "corpus.pdf") == input_hash and (output / "corpus.pdf").stat().st_size == corpus["bytes"], "Copied PDF differs from input")
    require(json.loads((output / "corpus.json").read_text(encoding="utf-8")) == corpus, "Corpus artifact differs")
    inventory = corpus["inventory"]
    require(len(inventory) == count, "Incomplete text inventory")
    for number, page in enumerate(inventory, 1):
        require(page["pageNumber"] == number and page["textFile"] == f"text/{number}.txt", "Invalid page identity")
        require(digest(output / page["textFile"]) == page["textSha256"], "Preserved extracted text differs")
    profile = report["profile"]
    require(profile["status"] == "passed" and profile["protocolVersion"] == 1 and profile["timeUnit"] == "microseconds", "Incomplete text pipeline")
    require(profile["rawLoadScope"] == "native-normalization-scheduling-transfer" and profile["cacheState"] == "structured-cache-reset-per-iteration", "Text scope differs")
    config = profile["configuration"]
    require(config["pageNumbers"] == list(range(1, count + 1)) and config["documentPageCount"] == count, "Different pages measured")
    require(config["warmup"] == warmup and config["repetitions"] == repetitions, "Different sample count")
    require(config["normalizeHebrewText"] is True and config["decodeLegacyHebrewText"] is True, "Different normalization policy")
    require(config["cache"] == {"maximumPages": 16, "maximumBytes": 8 * 1024 * 1024}, "Cache budget changed")
    require(tuple(query["id"] for query in config["queries"]) == QUERY_IDS, "Query identity changed")
    rows = profile["observations"]
    expected = {(i, phase, page) for i in range(warmup + repetitions) for phase in PHASES for page in range(1, count + 1)}
    require(len(rows) == len(expected), "Missing native observations")
    seen = set()
    signatures = {}
    for row in rows:
        key = (row["iteration"], row["phase"], row["pageNumber"])
        require(key in expected and key not in seen and row["parity"] is True, "Missing, duplicate, or failed observation")
        seen.add(key)
        require(row["warmup"] == (row["iteration"] < warmup), "Warmup entered measured samples")
        page = inventory[row["pageNumber"] - 1]
        require(row["textSha256"] == page["textSha256"] and row["textCharacters"] == page["textCharacters"], "Measured text differs from inventory")
        require(row["rawLoadCalls"] == 1 and row["formatRawCalls"] == (1 if row["phase"] == "pipeline" else 0), "Raw extraction accounting differs")
        require(row["cachedPagesAfter"] <= 16 and row["cachedBytesAfter"] <= 8 * 1024 * 1024, "Cache budget exceeded")
        for name in TIMINGS:
            unavailable = name == ("cacheLoadUs" if row["phase"] == "pipeline" else "formatUs")
            require(row[name] is None if unavailable else type(row[name]) is int and row[name] >= 0, f"Invalid timing: {name}")
        require(tuple(query["id"] for query in row["queries"]) == QUERY_IDS, "Missing query results")
        for query in row["queries"]:
            require(type(query["matches"]) is int and query["matches"] >= 0 and type(query["rects"]) is int and query["rects"] >= 0, "Invalid result counts")
            if query["id"] in ("no_match", "zero_length"):
                require(query["matches"] == 0 and query["rects"] == 0, "Control query unexpectedly matched")
        signature = json.dumps([row[name] for name in ("textSha256", "geometrySha256", "matchesSha256", "queries")], sort_keys=True)
        require(signatures.setdefault(row["pageNumber"], signature) == signature, "Text/geometry/results changed across passes")
    require([json.loads(line) for line in (output / "observations.jsonl").read_text(encoding="utf-8").splitlines()] == rows, "Raw JSONL differs from report")
    summary = {}
    for phase in PHASES:
        values = [row for row in rows if row["phase"] == phase and not row["warmup"]]
        summary[phase] = {
            name: distribution([sum(row[name] for row in values if row["iteration"] == iteration) for iteration in range(warmup, warmup + repetitions)])
            if any(row[name] is not None for row in values) else None for name in TIMINGS
        }
        summary[phase]["rawLoadCalls"] = sum(row["rawLoadCalls"] for row in values)
    require(summary == profile["summary"], "Summary is not derived from complete iteration totals")
    observations = report["textObservations"]
    totals = {query: sum(next(value["matches"] for value in row["queries"] if value["id"] == query) for row in rows if row["iteration"] == 0 and row["phase"] == "pipeline") for query in QUERY_IDS}
    require(observations["queryMatchesPerCompletePass"] == totals, "Diagnostic query totals differ from raw observations")
    require(observations["sourcePdfEncodedSpaceClaims"] is False, "Extraction does not identify encoded PDF space characters")
    raw = observations["rawBeforeHebrewNormalization"]
    require(raw["normalizeHebrewText"] is False and raw["decodeLegacyHebrewText"] is False and raw["pageNumber"] == 1, "Raw normalization probe changed")
    require(raw["file"] == "text/raw-page-1-before-hebrew-normalization.txt" and digest(output / raw["file"]) == raw["sha256"], "Raw extraction artifact differs")
    require(not (output / "failure.json").exists(), "Native run recorded a failure")


def main():
    parser = argparse.ArgumentParser()
    for name in ("dart", "pdfium", "input-pdf", "output", "flutter-json"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--packages", type=Path)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--document-label", required=True)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=7)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    require(not output.exists(), "Output directory must be new")
    output.mkdir(parents=True)
    try:
        require(platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64"), "PDFium receipt supports Linux x64 only")
        require(args.warmup >= 0 and args.repetitions >= 1, "Invalid repetition count")
        require(re.fullmatch(r"[0-9a-f]{64}", args.input_sha256) is not None, "Invalid input SHA-256")
        dart, pdfium, input_pdf = (path.resolve(strict=True) for path in (args.dart, args.pdfium, args.input_pdf))
        packages = (args.packages or repo / ".dart_tool/package_config.json").resolve(strict=True)
        require(digest(pdfium) == common.PDFIUM_SHA256 and digest(input_pdf) == args.input_sha256, "Input PDF or PDFium identity differs")
        before = source_receipt(repo)
        sdk = common.sdk_receipt(run([dart, "--version"], repo), args.flutter_json)
        toolchain = {"dartSha256": digest(dart), **common.package_resolution_receipt(repo, packages), "pdfiumSha256": digest(pdfium)}
        write_json(output / "source-before.json", before)
        write_json(output / "sdk.json", sdk)
        independent = independent_extraction(input_pdf, output, repo)
        executable, depfile = output / "external-text-profile", output / "compile.d"
        command = [dart, "compile", "exe", f"--packages={packages}", "-DPDF_TEXT_PROFILE_MODE=dart-aot", f"--depfile={depfile}", ENTRY_POINT, "-o", executable]
        write_json(output / "compile-command.json", [str(value) for value in command])
        run(command, repo, log=output / "compile.log", timeout=600)
        require(source_receipt(repo) == before, "Source changed during compilation")
        dependencies = compiler_dependencies(depfile, repo)
        write_json(output / "compiler-inputs-after-build.json", dependencies)
        executable_hash = digest(executable)
        native = output / "native"
        run([executable, "--output", native, "--input-pdf", input_pdf, "--input-sha256", args.input_sha256, "--document-label", args.document_label, "--pdfium", pdfium, "--pdfium-sha256", common.PDFIUM_SHA256, "--warmup", args.warmup, "--repetitions", args.repetitions], repo, log=output / "native.log", timeout=1800)
        report = json.loads((native / "report.json").read_text(encoding="utf-8"))
        validate_native_report(report, native, input_hash=args.input_sha256, label=args.document_label, warmup=args.warmup, repetitions=args.repetitions)
        after = source_receipt(repo)
        write_json(output / "source-after.json", after)
        require(before == after and compiler_dependencies(depfile, repo) == dependencies, "Source/compiler inputs changed during execution")
        require(toolchain == {"dartSha256": digest(dart), **common.package_resolution_receipt(repo, packages), "pdfiumSha256": digest(pdfium)} and digest(executable) == executable_hash and digest(input_pdf) == args.input_sha256, "Runtime, executable, or input PDF changed")
        write_json(output / "validated.json", {
            "protocolVersion": 1, "status": "passed", "executionMode": "dart-aot",
            "source": before, "sdk": sdk, "toolchain": toolchain,
            "executableSha256": executable_hash,
            "host": {"system": platform.platform(), "machine": platform.machine(), "processors": os.cpu_count()},
            "reportSha256": digest(native / "report.json"), "corpusSha256": args.input_sha256,
            "summary": report["profile"]["summary"],
            "textObservations": report["textObservations"], "independentExtraction": independent,
        })
        print(json.dumps({"status": "passed", "output": str(output), "commit": before["commit"]}))
        return 0
    except Exception as error:
        write_json(output / "failure.json", {"status": "failed", "error": str(error), "stack": traceback.format_exc()})
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

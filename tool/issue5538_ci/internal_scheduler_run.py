"""Compare the narrow worker backport on Otzaria's unchanged Dart 3.13.5 SDK."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import traceback
from urllib.parse import unquote, urlparse

import internal_upstream_run as common


REPOSITORY = "https://github.com/yom-chadash/pdfrx.git"
SOURCES = {
    "before": {"commit": "964b5e15e082c8633651ffab1d037631af65e156",
               "tree": "dc94d5cbc9d2ded39c18cb4036b32c2a42a81b8e",
               "workerSha256": "bfa4467064f046e223c0dd05f1217414b51bf9bc1287c96340d053a090e6cda5"},
    "after": {"commit": "af180fa24bd40f7ad422338b7c1f7e7c342b4a88",
              "tree": "d4c653d7d6137b2136d2d012986eaba65d3b042e",
              "workerSha256": "a32a2970c344f25d221ec6921ef7fe7fac8c549a7295e409d6131cabc007419e"},
}
SDK_REVISION = "abaf9c523780a608bd46686fd5e53740a07077f8"
ENGINE_REVISION = "deb287481e3ce9468f3434937ced4240a70539ca"
PDFIUM_SHA256 = "1c251efce460d67437a4c76d4be9033ae2f17bf5603f5bff1af10209b94ca723"
WORKER = "packages/pdfrx_engine/lib/src/native/worker.dart"
REGRESSION = "packages/pdfrx_engine/test/background_worker_cancellation_queue_test.dart"
REGRESSION_SHA256 = "0aad33309c490eb3903f0606b8016334c0fd680988c55967891e3a5da1aea1fb"
FIXTURES = (("normal", 100), ("normal", 1000), ("normal", 4096), ("normal", 10000),
            ("mixed", 4096), ("mixed", 10000), ("cooperative", 1000))


def tree_inventory(source):
    data = subprocess.check_output(["git", "ls-tree", "-r", "-z", "HEAD"], cwd=source, timeout=60)
    result = {}
    for entry in data.split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            result[name.decode("utf-8")] = metadata.decode("ascii")
    return result


def verify_delta(before, after):
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    common.require(changed == {WORKER, REGRESSION}, "Candidate changes more than the worker and its regression")
    common.require(REGRESSION not in before and REGRESSION in after, "Expected one new regression file")
    return sorted(changed)


def dependency_identity(source):
    config = source / ".dart_tool/package_config.json"
    packages = json.loads(config.read_text(encoding="utf-8"))["packages"]
    result = {}
    for item in packages:
        uri = urlparse(item["rootUri"])
        common.require(uri.scheme in ("", "file"), "Unexpected dependency URI")
        path = Path(unquote(uri.path)) if uri.scheme else config.parent / unquote(item["rootUri"])
        path = path.resolve()
        identity = "source:" + path.relative_to(source).as_posix() if path.is_relative_to(source) else str(path)
        result[item["name"]] = {"root": identity, "packageUri": item.get("packageUri"),
                                 "languageVersion": item.get("languageVersion")}
    return result


def parse_observation(text, field):
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and field in row:
            rows.append(row)
    common.require(len(rows) == 1, "Expected one complete observation")
    return rows[0]


def validate_sample(sample, mode, requests):
    cooperative = requests if mode == "cooperative" else ((requests + 99) // 100 if mode == "mixed" else 0)
    fixture = {"mode": mode, "requests": requests, "cooperativeRequests": cooperative, "steps": 3}
    expected_hash = hashlib.sha256(json.dumps(fixture, separators=(",", ":")).encode()).hexdigest()
    common.require(sample.get("fixture") == fixture and sample.get("fixtureSha256") == expected_hash,
                   "Timed fixture or workload changed")
    common.require(sample.get("cooperativeSteps") == cooperative * 3 and
                   sample.get("cooperativeDisposals") == cooperative, "Lost or duplicate cooperative work")
    common.require(sample.get("dart", "").startswith("3.13.5 ") and sample.get("os") == "linux",
                   "Timed executable used a different SDK or platform")
    common.require(isinstance(sample.get("wallMicros"), int) and sample["wallMicros"] > 0,
                   "Timed sample has no elapsed duration")


def validate_scan_pair(before, after, mode, requests):
    fields = ("mode", "requests", "steps", "cooperativeRequests", "cooperativeCancellationReads",
              "ordinaryCompletions", "cooperativeSteps", "cooperativeDisposals")
    common.require(all(before.get(k) == after.get(k) for k in fields), "Scan workload or cooperative behavior changed")
    cooperative = requests if mode == "cooperative" else ((requests + 99) // 100 if mode == "mixed" else 0)
    common.require(before.get("mode") == mode and before.get("requests") == requests and
                   before.get("steps") == 3 and before.get("cooperativeRequests") == cooperative and
                   before.get("ordinaryCompletions") == requests - cooperative and
                   before.get("cooperativeSteps") == cooperative * 3 and
                   before.get("cooperativeDisposals") == cooperative, "Incomplete count workload")
    common.require(after.get("ordinaryCancellationReads") == 0, "Candidate still scans ordinary cancellation")
    if mode == "normal":
        common.require(before.get("ordinaryCancellationReads") == requests * (requests + 1) // 2,
                       "Baseline does not match the expected prequeued cancellation scan")
    elif mode == "mixed":
        common.require(before.get("ordinaryCancellationReads", 0) > 0, "Mixed baseline did not scan ordinary work")
    else:
        common.require(before.get("ordinaryCancellationReads") == 0, "Cooperative fixture contained ordinary work")


def summary(samples):
    result = []
    for mode, requests in FIXTURES:
        values = {}
        for version in SOURCES:
            selected = [row for row in samples if row["version"] == version and not row["warmup"] and
                        row["fixture"]["mode"] == mode and row["fixture"]["requests"] == requests]
            common.require(len(selected) == 5, "Expected five retained fresh-process samples per variant")
            values[version] = {
                metric: {"median": statistics.median(row[metric] for row in selected),
                         "min": min(row[metric] for row in selected), "max": max(row[metric] for row in selected)}
                for metric in ("wallMicros", "processCpuMicros", "peakRssBytes")
            }
        result.append({"mode": mode, "requests": requests, "variants": values,
                       "medianWallRatioAfterBefore": values["after"]["wallMicros"]["median"] /
                                                      values["before"]["wallMicros"]["median"]})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flutter", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    common.require(os.environ.get("CI") == "true" and
                   os.environ.get("GITHUB_REPOSITORY") == "yom-chadash/otzaria", "Authorized fork CI only")
    common.require(platform.system() == "Linux" and platform.machine() == "x86_64", "Linux x64 lane required")
    module = Path(__file__).resolve().parent
    root = module.parents[1]
    output = args.output.resolve()
    common.require(args.output.is_absolute() and not output.exists() and not output.is_relative_to(root),
                   "Output must be a new absolute directory outside the fork checkout")
    output.mkdir(parents=True)
    receipts = output / "receipts"
    samples_dir = output / "samples"
    work = output / "work"
    for directory in (receipts, samples_dir, work):
        directory.mkdir()
    try:
        sdk = json.loads(common.run([args.flutter, "--version", "--machine"], root, receipts / "flutter.json"))
        common.require(sdk.get("frameworkVersion") == "3.47.7" and sdk.get("frameworkRevision") == SDK_REVISION and
                       sdk.get("engineRevision") == ENGINE_REVISION and sdk.get("dartSdkVersion") == "3.13.5",
                       "The application SDK pin changed")
        dart = args.flutter.resolve().parent / "cache/dart-sdk/bin/dart"
        fork = common.run(["git", "rev-parse", "HEAD"], root, receipts / "fork-sha.log").strip()
        common.require(not common.run(["git", "status", "--porcelain", "--untracked-files=all"], root,
                                      receipts / "fork-status.log"), "Fork source is dirty")
        sources = {}
        inventories = {}
        for version, identity in SOURCES.items():
            source = work / version
            source.mkdir()
            receipt = receipts / version
            receipt.mkdir()
            common.run(["git", "init"], source, receipt / "git-init.log")
            common.run(["git", "config", "core.autocrlf", "false"], source, receipt / "git-line-endings.log")
            common.run(["git", "fetch", "--depth=1", REPOSITORY, identity["commit"]], source, receipt / "git-fetch.log")
            common.run(["git", "checkout", "--detach", "FETCH_HEAD"], source, receipt / "git-checkout.log")
            actual = common.run(["git", "rev-parse", "HEAD", "HEAD^{tree}"], source, receipt / "source.log").splitlines()
            common.require(actual == [identity["commit"], identity["tree"]], "Frozen source identity changed")
            common.require(common.digest(source / WORKER) == identity["workerSha256"], "Worker source changed")
            sources[version] = source
            inventories[version] = tree_inventory(source)
        changed = verify_delta(inventories["before"], inventories["after"])
        regression = sources["after"] / REGRESSION
        common.require(common.digest(regression) == REGRESSION_SHA256, "Nine regression cases changed")
        patch = ""
        for name in changed:
            before_file = sources["before"] / name
            before = before_file.read_text(encoding="utf-8") if before_file.exists() else ""
            after = (sources["after"] / name).read_text(encoding="utf-8")
            patch += "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                fromfile="a/" + name, tofile="b/" + name))
        (receipts / "backport.patch").write_text(patch, encoding="utf-8")
        provenance = {"forkCommit": fork, "sources": SOURCES, "sourceDelta": changed,
                      "runtimeDelta": [WORKER], "patchSha256": common.digest(receipts / "backport.patch"),
                      "flutter": sdk,
                      "platform": platform.platform(), "processors": os.cpu_count(),
                      "runId": os.environ.get("GITHUB_RUN_ID"), "job": os.environ.get("GITHUB_JOB"),
                      "fixtureOrigin": "espresso3389/pdfrx#880 at 7aa9ed50d1453c3c43ba349ffe41ee1f9eace642",
                      "fixtureSourceSha256": common.digest(module / "internal_scheduler_benchmark.dart.template"),
                      "harness": {p.name: common.digest(p) for p in
                                  [Path(__file__), module / "internal_scheduler_scan.py",
                                   module / "internal_upstream_run.py"]},
                      "scope": "Synthetic prequeued work on actual worker isolate; no PDF viewer or UI timing.",
                      "samplePolicy": "One fresh-process warmup each, then five samples each in alternating AB/BA order.",
                      "counterPolicy": "Separate instrumented operation-count probe; never used for timing ratios."}
        common.write_json(receipts / "provenance.json", provenance)
        executables = {}
        frozen = {}
        dependency_sets = {}
        test_receipts = {}
        for version, source in sources.items():
            receipt = receipts / version
            if version == "before":
                common.run([args.flutter, "pub", "get"], source, receipt / "pub-get.log")
            else:
                shutil.copyfile(sources["before"] / "pubspec.lock", source / "pubspec.lock")
            common.run([args.flutter, "pub", "get", "--enforce-lockfile"], source, receipt / "pub-get-enforced.log")
            frozen[version] = common.freeze_resolved_source(source, receipt)
            common.require(not common.run(["git", "status", "--porcelain", "--untracked-files=all"], source,
                                          receipt / "source-clean.log"), "Resolved checkout has unexpected source files")
            shutil.copyfile(source / "pubspec.lock", receipt / "pubspec.lock")
            dependency_sets[version] = dependency_identity(source)
            common.write_json(receipt / "dependency-identity.json", dependency_sets[version])
            if version == "after":
                common.require(common.digest(source / "pubspec.lock") ==
                               common.digest(sources["before"] / "pubspec.lock") and
                               dependency_sets["before"] == dependency_sets["after"], "A/B dependencies differ")
            package = source / "packages/pdfrx_engine"
            temporary = package / ".dart_tool/issue5538-scheduler"
            temporary.mkdir(parents=True)
            entry = temporary / "benchmark.dart"
            test = temporary / "background_worker_cancellation_queue_test.dart"
            shutil.copyfile(module / "internal_scheduler_benchmark.dart.template", entry)
            shutil.copyfile(regression, test)
            common.run([dart, "analyze", entry, test], package, receipt / "analyze.log")
            test_log = common.run([dart, "test", "--concurrency=1", "--reporter=json",
                                   "test/background_worker_test.dart", "test/background_worker_task_test.dart", test],
                                  package, receipt / "worker-tests.jsonl")
            test_receipts[version] = common.json_test_receipt(test_log)
            common.require(test_receipts[version]["executed"] == 41 and test_receipts[version]["skipped"] == 0,
                           "The complete worker ordering/cancellation/failure/disposal suite must execute")
            executable = work / (version + "-worker-benchmark")
            common.run([dart, "compile", "exe", entry, "-o", executable], package, receipt / "compile.log")
            common.require(common.digest(entry) == provenance["fixtureSourceSha256"], "Compiled benchmark changed")
            pdfium = source / ".dart_tool/lib/libpdfium.so"
            common.require(pdfium.is_file() and common.digest(pdfium) == PDFIUM_SHA256, "PDFium binary changed")
            executables[version] = executable
            common.write_json(receipt / "executable.json", {"sha256": common.digest(executable),
                              "bytes": executable.stat().st_size, "workerSha256": SOURCES[version]["workerSha256"],
                              "fixtureSha256": common.digest(entry), "pdfiumSha256": common.digest(pdfium)})
            common.verify_frozen_source(source, receipt, frozen[version], "after-build")
        samples = []
        scans = []
        for mode, requests in FIXTURES:
            for sample_index in range(-1, 5):
                order = ("before", "after") if sample_index < 0 or sample_index % 2 == 0 else ("after", "before")
                for version in order:
                    name = f"{mode}-{requests}-{sample_index}-{version}"
                    raw = common.run([executables[version], mode, str(requests), "3"], sources[version],
                                     samples_dir / (name + ".log"), timeout=300)
                    sample = parse_observation(raw, "fixture")
                    validate_sample(sample, mode, requests)
                    sample.update({"version": version, "sample": sample_index, "warmup": sample_index < 0,
                                   "executableSha256": common.digest(executables[version])})
                    samples.append(sample)
                    common.write_json(samples_dir / (name + ".json"), sample)
            pair = {}
            for version, source in sources.items():
                name = f"{mode}-{requests}-{version}-scan"
                raw = common.run([sys.executable, module / "internal_scheduler_scan.py", mode, str(requests),
                                  "--engine-root", source / "packages/pdfrx_engine", "--source", source / WORKER,
                                  "--dart", dart, "--receipt-directory", receipts / version / "scan"],
                                 root, samples_dir / (name + ".log"), timeout=600)
                pair[version] = parse_observation(raw, "ordinaryCancellationReads")
                common.require(pair[version]["workerSourceSha256"] == SOURCES[version]["workerSha256"],
                               "Scan used an unexpected worker source")
                scans.append({**pair[version], "version": version})
                common.write_json(samples_dir / (name + ".json"), scans[-1])
            validate_scan_pair(pair["before"], pair["after"], mode, requests)
        for version, source in sources.items():
            common.verify_frozen_source(source, receipts / version, frozen[version], "after-measurements")
            common.require(not common.run(["git", "status", "--porcelain", "--untracked-files=all"], source,
                                          receipts / version / "source-final-status.log"), "Measured source became dirty")
            identity = json.loads((receipts / version / "executable.json").read_text())
            common.require(common.digest(executables[version]) == identity["sha256"], "Timed executable changed")
        common.write_json(output / "summary.json", {"complete": True, "sources": SOURCES, "tests": test_receipts,
                          "warmupProcesses": 14, "measuredProcesses": 70, "countProbes": len(scans),
                          "results": summary(samples), "operationCounts": scans,
                          "limitations": ["Synthetic queued work, not UI responsiveness, PDF raster time or device evidence.",
                                          "Cooperative-only cancellation polling remains quadratic.",
                                          "All timing outliers are retained; no timing threshold gates correctness."]})
    except Exception as error:
        common.write_json(output / "failure.json", {"complete": False, "error": str(error),
                                                     "traceback": traceback.format_exc()})
        raise


if __name__ == "__main__":
    main()

"""Build two identified app revisions once, then run identical ABBA profiles."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import traceback

import run_pdf_text_profile as common
import validate_pdf_highlight_profile as validation


TARGET = "integration_test/pdf_highlight_profile_test.dart"
DRIVER = "test_driver/integration_test.dart"
HIGHLIGHTS = "lib/pdf_book/utils/pdf_page_highlights.dart"
LAYOUT = "lib/pdf_book/utils/pdf_book_layout.dart"
HARNESS = (
    TARGET, DRIVER,
    "integration_test/support/pdf_proximity_profile_queries.dart",
    "tool/perf_probe/run_pdf_highlight_pair.py",
    "tool/perf_probe/validate_pdf_highlight_profile.py",
    "tool/perf_probe/validate_pdf_proximity_profile.py",
    "tool/perf_probe/run_pdf_text_profile.py",
)
SHARED = (*HARNESS, LAYOUT, "pubspec.yaml", "pubspec.lock",
          "lib/pdf_book/utils/result_lines_highlight_pattern.dart",
          "lib/pdf_book/utils/proximity_highlight_pattern.dart",
          "lib/pdf_book/view/pdf_search_screen.dart",
          "lib/search/in_book_search_settings.dart",
          "lib/search/search_engine_library.dart",
          "lib/search/models/search_configuration.dart",
          "lib/search/models/search_match_policy.dart",
          "lib/search/utils/in_book_search_routing.dart",
          "lib/utils/text/text_manipulation.dart")
ALLOWED_DELTA = {HIGHLIGHTS, "test/pdf_book/pdf_page_highlights_test.dart"}
require, digest, write_json = common.require, common.digest, common.write_json


def run(arguments, repo, *, log=None, timeout=600, environment=None):
    env = {**os.environ, "CI": "true", **(environment or {})}
    try:
        result = subprocess.run([str(value) for value in arguments], cwd=repo, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if log is not None:
            value = error.stdout or b""
            log.write_bytes(value if isinstance(value, bytes) else value.encode("utf-8"))
        raise
    if log is not None:
        log.write_text(result.stdout, encoding="utf-8")
    require(result.returncode == 0, f"Command failed ({result.returncode}): {arguments}\n{result.stdout[-6000:]}")
    return result.stdout


def load_plan(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    for side in ("baseline", "candidate"):
        require(isinstance(value.get(side), str) and re.fullmatch(r"[0-9a-f]{40}", value[side]) is not None,
                f"Plan {side} must identify an immutable full commit SHA")
    require(value["baseline"] != value["candidate"], "Baseline and candidate must be different commits")
    return {side: value[side] for side in ("baseline", "candidate")}


def load_documents(path, selected):
    root = path.parent.resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    documents = manifest["documents"]
    require(isinstance(documents, list) and len({doc["id"] for doc in documents}) == len(documents), "Invalid or duplicate corpus IDs")
    result = []
    for label in selected:
        require(re.fullmatch(r"[a-zA-Z0-9_-]+", label) is not None, "Invalid document ID")
        matches = [doc for doc in documents if doc["id"] == label]
        require(len(matches) == 1, f"Missing document: {label}")
        document = matches[0]
        requested = Path(document["path"])
        require(not requested.is_absolute(), "Corpus paths must be relative to the manifest")
        input_pdf = (root / requested).resolve(strict=True)
        require(input_pdf.is_relative_to(root) and input_pdf.is_file(), "Corpus file escapes the manifest directory")
        require(digest(input_pdf) == document["sha256"] and input_pdf.stat().st_size == document["sizeBytes"], "Corpus hash/length differs from its manifest")
        require(type(document["pageCount"]) is int and document["pageCount"] > 0, "Invalid source page count")
        result.append({**document, "absolutePath": str(input_pdf)})
    require(len({doc["id"] for doc in result}) == len(result), "Repeated selected corpus")
    return result


def source_receipt(repo):
    require(not run(["git", "status", "--porcelain", "--untracked-files=all"], repo), "Source checkout is not clean")
    return {
        "commit": run(["git", "rev-parse", "HEAD"], repo).strip(),
        "tree": run(["git", "rev-parse", "HEAD^{tree}"], repo).strip(),
        "files": {name: digest(repo / name) for name in (*SHARED, HIGHLIGHTS)},
    }


def compiler_receipt(repo):
    candidates = []
    for depfile in (repo / ".dart_tool/flutter_build").glob("*/kernel_snapshot_program.d"):
        text = depfile.read_text(encoding="utf-8").replace("\\\n", "")
        if ": " not in text:
            continue
        paths = set()
        for value in shlex.split(text.split(": ", 1)[1]):
            path = Path(value)
            paths.add((path if path.is_absolute() else repo / path).resolve())
        required = (TARGET, HIGHLIGHTS,
                    "integration_test/support/pdf_proximity_profile_queries.dart",
                    "lib/pdf_book/view/pdf_search_screen.dart",
                    "lib/pdf_book/utils/proximity_highlight_pattern.dart",
                    "lib/search/search_engine_library.dart")
        if all(repo / name in paths for name in required):
            candidates.append((depfile, paths))
    require(len(candidates) == 1, "Need exactly one compiler depfile containing the real app helper and benchmark entry point")
    depfile, paths = candidates[0]
    return {"depfile": str(depfile), "depfileSha256": digest(depfile),
            "inputs": {str(path): digest(path) for path in sorted(paths)}}


def bundle_receipt(bundle):
    required = (bundle / "otzaria", bundle / "lib/libapp.so", bundle / "lib/libflutter_linux_gtk.so",
                bundle / "lib/libsearch_engine.so")
    require(all(path.is_file() for path in required), "Profile bundle lacks executable, AOT app, Flutter engine, or native search library")
    return {path.relative_to(bundle).as_posix(): {"sha256": digest(path), "bytes": path.stat().st_size}
            for path in sorted(bundle.rglob("*")) if path.is_file()}


def validate_source_pair(before, after, changed):
    require(set(changed) <= ALLOWED_DELTA and HIGHLIGHTS in changed, "Pair changes more than app highlights and their correctness tests")
    require(all(before["files"][name] == after["files"][name] for name in SHARED), "Harness, layout, patterns, SDK constraints, or lockfile differ across the pair")
    require(before["files"][HIGHLIGHTS] != after["files"][HIGHLIGHTS], "App helper is unchanged")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pdfium", type=Path, required=True)
    parser.add_argument("--flutter", type=Path, default=Path(shutil.which("flutter") or "flutter"))
    parser.add_argument("--documents", nargs="+", default=["uxlc-genesis", "birkon-3_0"])
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=7)
    parser.add_argument("--blocks", type=int, default=1)
    parser.add_argument("--query-suite", choices=("legacy-v1", validation.proximity.SUITE), default="legacy-v1")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    require(not output.exists(), "Output directory must be new")
    output.mkdir(parents=True)
    checkouts = []
    checkout_root = Path(tempfile.mkdtemp(prefix="pdf-highlight-pair-", dir=output.parent))
    try:
        require(platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64"), "Profile pairing currently pins Linux x64")
        require(args.warmup >= 0 and args.repetitions > 0, "Invalid sample counts")
        require(args.blocks > 0, "ABBA block count must be positive")
        require(args.query_suite != validation.proximity.SUITE or args.blocks >= 3, "Product Proximity requires at least three fresh-process ABBA blocks")
        launch_order = validation.proximity.ABBA * args.blocks
        plan = load_plan(args.plan.resolve(strict=True))
        manifest = args.corpus.resolve(strict=True)
        manifest_sha = digest(manifest)
        documents = load_documents(manifest, args.documents)
        pdfium = args.pdfium.resolve(strict=True)
        require(digest(pdfium) == common.PDFIUM_SHA256, "Unexpected native PDFium library")
        flutter = args.flutter.resolve(strict=True)
        dart = flutter.parent / "cache/dart-sdk/bin/dart"
        sdk_json = output / "flutter.json"
        sdk_json.write_text(run([flutter, "--version", "--machine"], repo), encoding="utf-8")
        sdk = common.sdk_receipt(run([dart, "--version"], repo), sdk_json)
        toolchain = {"flutterSha256": digest(flutter), "dartSha256": digest(dart), "pdfiumSha256": digest(pdfium), "sdk": sdk}
        write_json(output / "plan.json", plan)
        write_json(output / "sampling-plan.json", {
            "querySuite": args.query_suite, "blocksPerCorpus": args.blocks,
            "launchOrderPerCorpus": launch_order, "processesPerSidePerCorpus": 2 * args.blocks,
            "warmupPassesPerProcess": args.warmup, "measuredPassesPerProcess": args.repetitions,
            "statisticalUnit": "fresh app process; repeated passes within a process are dependent",
            "builds": 2, "binaryReuse": "one verified AOT bundle per source across all corpora and blocks",
        })
        write_json(output / "corpus-manifest.json", json.loads(manifest.read_text(encoding="utf-8")))
        write_json(output / "host.json", {"name": socket.gethostname(), "platform": platform.platform(), "processors": os.cpu_count()})
        receipts = {}
        for side, commit in plan.items():
            exists = subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if exists.returncode != 0:
                run(["git", "fetch", "--depth=1", "--no-tags", "origin", commit], repo)
            checkout = checkout_root / side
            run(["git", "worktree", "add", "--detach", checkout, commit], repo)
            checkouts.append(checkout)
            receipt = source_receipt(checkout)
            require(receipt["commit"] == commit, "Detached checkout does not match plan")
            receipts[side] = {"checkout": checkout, "source": receipt}
        changed = run(["git", "diff", "--name-only", plan["baseline"], plan["candidate"]], repo).splitlines()
        validate_source_pair(receipts["baseline"]["source"], receipts["candidate"]["source"], changed)
        for name in HARNESS:
            require(digest(repo / name) == receipts["baseline"]["source"]["files"][name], f"Invoked harness differs from measured harness: {name}")
        for side, receipt in receipts.items():
            checkout = receipt["checkout"]
            build = output / "build" / side
            build.mkdir(parents=True)
            run([flutter, "pub", "get", "--enforce-lockfile"], checkout, log=build / "pub-get.log")
            receipt["resolution"] = common.package_resolution_receipt(checkout, checkout / ".dart_tool/package_config.json")
            require(source_receipt(checkout) == receipt["source"], "Dependency resolution changed source")
            command = [flutter, "build", "linux", "--profile", "--no-pub", f"--target={TARGET}",
                       f"--dart-define=PDF_HIGHLIGHT_PROFILE_SOURCE_COMMIT={receipt['source']['commit']}",
                       f"--dart-define=PDF_HIGHLIGHT_PROFILE_SOURCE_SHA256={receipt['source']['files'][HIGHLIGHTS]}",
                       f"--dart-define=PDF_HIGHLIGHT_PROFILE_WARMUP={args.warmup}",
                       f"--dart-define=PDF_HIGHLIGHT_PROFILE_REPETITIONS={args.repetitions}"]
            write_json(build / "build-command.json", [str(value) for value in command])
            run(command, checkout, log=build / "build.log", timeout=2400)
            require(source_receipt(checkout) == receipt["source"], "Build changed source")
            receipt["compiler"] = compiler_receipt(checkout)
            receipt["bundlePath"] = checkout / "build/linux/x64/profile/bundle"
            receipt["bundle"] = bundle_receipt(receipt["bundlePath"])
            write_json(build / "source.json", receipt["source"])
            write_json(build / "resolution.json", receipt["resolution"])
            write_json(build / "compiler-inputs.json", receipt["compiler"])
            write_json(build / "bundle.json", receipt["bundle"])
        require(receipts["baseline"]["bundle"]["lib/libsearch_engine.so"] == receipts["candidate"]["bundle"]["lib/libsearch_engine.so"],
                "Native search engine differs between baseline and candidate builds")
        all_results = {}
        for document in documents:
            completed = {"baseline": [], "candidate": []}
            paths = {"baseline": [], "candidate": []}
            launches = []
            for index, side in enumerate(launch_order):
                receipt = receipts[side]
                checkout = receipt["checkout"]
                run_root = output / document["id"] / f"{index + 1}-{side}"
                run_root.mkdir(parents=True)
                native = run_root / "native"
                environment = {
                    "PDF_HIGHLIGHT_PROFILE_INPUT": document["absolutePath"],
                    "PDF_HIGHLIGHT_PROFILE_SHA256": document["sha256"],
                    "PDF_HIGHLIGHT_PROFILE_PDFIUM": str(pdfium),
                    "PDF_HIGHLIGHT_PROFILE_OUTPUT": str(native),
                    "PDF_HIGHLIGHT_PROFILE_QUERY_SUITE": args.query_suite,
                }
                require(source_receipt(checkout) == receipt["source"] and compiler_receipt(checkout) == receipt["compiler"], "Source or compiler inputs changed before launch")
                require(bundle_receipt(receipt["bundlePath"]) == receipt["bundle"], "Profile binary changed before launch")
                command = [flutter, "drive", "--profile", "--no-pub", "-d", "linux", f"--driver={DRIVER}", f"--target={TARGET}",
                           f"--use-application-binary={receipt['bundlePath'] / 'otzaria'}"]
                if not os.environ.get("DISPLAY"):
                    xvfb = shutil.which("xvfb-run")
                    require(xvfb is not None, "Headless profile launch requires xvfb-run")
                    command = [xvfb, "-a", "-s", "-screen 0 1280x900x24", *command]
                write_json(run_root / "launch-command.json", [str(value) for value in command])
                write_json(run_root / "launch-inputs.json", environment)
                run(command, checkout, log=run_root / "drive.log", timeout=1200, environment=environment)
                require(source_receipt(checkout) == receipt["source"] and compiler_receipt(checkout) == receipt["compiler"], "Source or compiler inputs changed during launch")
                require(bundle_receipt(receipt["bundlePath"]) == receipt["bundle"], "Profile binary changed during launch")
                require(common.package_resolution_receipt(checkout, checkout / ".dart_tool/package_config.json") == receipt["resolution"], "Package resolution changed during launch")
                report_path = native / "report.json"
                report = validation.validate_report(report_path)
                require(report["sourceCommit"] == plan[side] and report["highlightSourceSha256"] == receipt["source"]["files"][HIGHLIGHTS], "Runtime source identity does not match the built source")
                require(report["inputSha256"] == document["sha256"] and report["sourcePageCount"] == document["pageCount"], "Runtime corpus identity differs from manifest")
                require(report["warmup"] == args.warmup and report["repetitions"] == args.repetitions, "Compiled sample configuration differs")
                require(report.get("querySuite") == args.query_suite, "Runtime query suite differs from the sampling plan")
                if args.query_suite == validation.proximity.SUITE:
                    require(report["searchEngine"]["librarySha256"] == receipt["bundle"]["lib/libsearch_engine.so"]["sha256"] and
                            report["searchEngine"]["libraryBytes"] == receipt["bundle"]["lib/libsearch_engine.so"]["bytes"],
                            "Actually mapped search engine differs from the recorded AOT bundle")
                completed[side].append(report)
                paths[side].append(report_path)
                launches.append({"side": side, "report": report, "reportPath": str(report_path.relative_to(output)), "reportSha256": digest(report_path)})
                write_json(run_root / "validated.json", {"status": "passed", "source": receipt["source"], "reportSha256": digest(report_path),
                                                        "launchIndex": index, "block": index // 4 + 1, "positionInBlock": index % 4 + 1, "side": side,
                                                        "querySuite": args.query_suite})
            comparison = validation.compare(completed["baseline"], completed["candidate"])
            comparison["launchOrder"] = launch_order
            comparison["processBlocks"] = validation.proximity.summarize_abba(launches)
            comparison["reports"] = {str(path.relative_to(output)): digest(path) for side in paths for path in paths[side]}
            write_json(output / document["id"] / "comparison.json", comparison)
            all_results[document["id"]] = comparison
        require(digest(manifest) == manifest_sha and load_documents(manifest, args.documents) == documents, "Corpus changed during paired run")
        require(toolchain == {"flutterSha256": digest(flutter), "dartSha256": digest(dart), "pdfiumSha256": digest(pdfium), "sdk": common.sdk_receipt(run([dart, "--version"], repo), sdk_json)}, "SDK/native binary changed")
        write_json(output / "validated.json", {"protocolVersion": 1, "status": "passed", "plan": plan,
                                               "toolchain": toolchain, "corpusManifestSha256": manifest_sha,
                                               "querySuite": args.query_suite, "blocksPerCorpus": args.blocks,
                                               "launchOrderPerCorpus": launch_order,
                                               "results": all_results})
        print(json.dumps({"status": "passed", "output": str(output), "corpora": list(all_results)}))
        return 0
    except Exception as error:
        write_json(output / "failure.json", {"status": "failed", "error": str(error), "stack": traceback.format_exc()})
        raise
    finally:
        for checkout in reversed(checkouts):
            subprocess.run(["git", "worktree", "remove", "--force", str(checkout)], cwd=repo,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if checkout_root.exists() and not any(checkout_root.iterdir()):
            checkout_root.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())

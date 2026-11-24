"""Fork-only app S02: immutable A/B/C sources, native identity and ABBA launches."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import tarfile
import time
import urllib.parse
import urllib.request

import yaml
from PIL import Image
from pypdf import PdfReader

from font_fallback_fixtures import SCENARIOS, prepare
from run_export_profile import (
    ROOT, FRAMEWORK_SHA, PDFIUM_SHA256, BundledFlutterEngine, binary_path,
    capture, child_exit, cleanup_group, digest, run_logged, save,
)
from run_copy_profile import native_mapping


TARGET = "tool/issue5538_ci/font_fallback_profile_app.dart"
HARNESS = (TARGET, "tool/issue5538_ci/font_fallback_fixtures.py",
           "tool/issue5538_ci/run_font_fallback_profile.py",
           "tool/issue5538_ci/run_copy_profile.py", "tool/issue5538_ci/run_export_profile.py",
           "tool/perf_probe/posix_engine.py", "tool/perf_probe/run_perf_probe_posix.py",
           "tool/perf_probe/build_receipt.py")
APP = ("lib/pdf_book/utils/pdf_font_fallback.dart",
       "lib/pdf_book/utils/pdf_document_font_recovery.dart",
       "lib/indexing/repository/indexing_repository.dart",
       "lib/library/view/book_preview_panel.dart")
TESTS = ("test/pdf_book/pdf_document_font_recovery_test.dart",
         "test/pdf_book/pdf_font_fallback_lifecycle_test.dart")
DEPENDENCIES = {"pubspec.yaml", "pubspec.lock"}
S02_COMMIT = "964b5e15e082c8633651ffab1d037631af65e156"
ENGINE_SHA = "deb287481e3ce9468f3434937ced4240a70539ca"
ARCHIVES = {
    "pdfrx": ("2.13.1", "69e2fdfae25c9686b336d89c3841766a1043bc5ba7f24bb8252224b837018440"),
    "pdfrx_engine": ("0.9.3", "001090d220b2773747ce396f2bcda3d6ac9389d87f937ef12d64ed3a9e80e8b0"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fixture_font_resources(item):
    reader = PdfReader(item["absolutePath"])
    actual = {}
    for number, page in enumerate(reader.pages, 1):
        fonts = page["/Resources"].get("/Font", {}).values()
        faces = sorted({str(font.get_object()["/BaseFont"])[1:] for font in fonts})
        if faces:
            actual[str(number)] = faces
    expected = {str(page): sorted(faces) for page, faces in item["fontResourcesByPage"].items()}
    require(actual == expected and len(reader.pages) == item["pageCount"],
            "Fixture font resources differ from its source-grounded manifest.")
    return actual


def font_query_key(query):
    require(isinstance(query.get("face"), str) and type(query.get("weight")) is int
            and type(query.get("italic")) is bool and type(query.get("charset")) is int
            and type(query.get("pitchFamily")) is int, "Incomplete five-field font query.")
    return tuple(query[key] for key in ("face", "weight", "italic", "charset", "pitchFamily"))


def validate_common_framework(builds):
    identities = {name: build["engine"].identity for name, build in builds.items()}
    require(set(identities) == {"A", "B", "C"}, "Framework evidence needs all three sources.")
    hashes = [identity.get("bundleSha256") for identity in identities.values()]
    require(all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes),
            "Framework evidence is missing its bundled binary SHA256.")
    require(len(set(hashes)) == 1, "Framework binary differs across A/B/C.")
    return identities


def validate_orchestrator(orchestrator, receipts):
    common = {name: orchestrator["files"][name] for name in HARNESS}
    require(all(common.values()) and all(
        {name: receipt["files"][name] for name in HARNESS} == common
        for receipt in receipts.values()), "Orchestrator harness differs from measured sources.")


def source(repo):
    require(not capture(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo).strip(),
            "Source must be clean.")
    files = {name: digest(repo / name) if (repo / name).is_file() else None
             for name in (*HARNESS, *APP, *TESTS, *sorted(DEPENDENCIES))}
    require(all(files[name] for name in HARNESS), "A shared harness input is missing.")
    return {"commit": capture(["git", "rev-parse", "HEAD"], cwd=repo).strip(),
            "tree": capture(["git", "rev-parse", "HEAD^{tree}"], cwd=repo).strip(), "files": files}


def validate_sources(receipts, roots):
    common = {name: receipts["A"]["files"][name] for name in HARNESS}
    require(all({name: item["files"][name] for name in HARNESS} == common for item in receipts.values()),
            "A/B/C must share every harness/host/native-verifier byte.")
    require(all(receipts["A"]["files"][name] == receipts["B"]["files"][name] for name in APP),
            "A to B must not change application font policy or integration.")
    require(all(receipts["B"]["files"][name] == receipts["C"]["files"][name] for name in DEPENDENCIES),
            "B to C must share exactly the same dependency pin.")
    for left, right, allowed in (("A", "B", DEPENDENCIES), ("B", "C", set(APP) | set(TESTS))):
        changed = set(capture(["git", "diff", "--name-only", receipts[left]["commit"], receipts[right]["commit"]], cwd=ROOT).splitlines())
        require(changed and changed <= allowed, f"Unexpected {left}/{right} causal delta: {changed - allowed}")
    locks = {name: yaml.safe_load((root / "pubspec.lock").read_text()) for name, root in roots.items()}
    require(all(lock["sdks"] == locks["A"]["sdks"] for lock in locks.values()), "SDK requirements changed.")
    for name, lock in locks.items():
        app_policy = (roots[name] / APP[0]).read_text().split("class _SystemFamilyResolver", 1)[1]
        builtin_policy = (roots[name] / TARGET).read_text().split("class _BuiltinSystemFamilyResolver", 1)[1]
        builtin_policy = builtin_policy.replace("_BuiltinSystemFamilyResolver", "_SystemFamilyResolver").replace(
            "_BuiltinBundledFontResolver", "_BundledFontResolver")
        require(builtin_policy == app_policy, "Builtin comparator resolver policy differs from the real app.")
        for package, version in (("pdfrx", "2.13.1"), ("pdfrx_engine", "0.9.3")):
            entry = lock["packages"][package]
            require(entry["version"] == version, "A newer PDF package was substituted.")
            if name == "A":
                require(entry["source"] == "hosted", "A must use the original hosted packages.")
            else:
                require(entry["source"] == "git" and entry["description"]["resolved-ref"] == S02_COMMIT
                        and entry["description"]["path"] == f"packages/{package}", "B/C use a different backport.")
    packages = set(locks["A"]["packages"]) | set(locks["B"]["packages"])
    require({key for key in packages if locks["A"]["packages"].get(key) != locks["B"]["packages"].get(key)}
            == {"pdfrx", "pdfrx_engine"}, "Unrelated dependency changed in engine comparison.")
    return {"AtoB": "engine backport only", "BtoC": "application integration only", "commonHarness": common}


def bundle_receipt(bundle):
    for name in ("otzaria", "lib/libapp.so", "lib/libflutter_linux_gtk.so"):
        require((bundle / name).is_file(), f"Missing executable/AOT/framework payload: {name}")
    result = {}
    for path in sorted(bundle.rglob("*")):
        name = str(path.relative_to(bundle))
        if path.is_symlink():
            target = path.resolve(strict=True)
            require(target.is_file(), "Directory/broken symlinks are not accepted in this bundle.")
            result[name] = {"kind": "symlink", "target": os.readlink(path), "resolvedPath": str(target),
                            "sha256": digest(target), "bytes": target.stat().st_size}
        elif path.is_file():
            result[name] = {"kind": "file", "sha256": digest(path), "bytes": path.stat().st_size,
                            "mode": stat.S_IMODE(path.stat().st_mode)}
    return result


def compiler_receipt(repo):
    matches = []
    for depfile in (repo / ".dart_tool/flutter_build").glob("*/kernel_snapshot_program.d"):
        raw = depfile.read_text().replace("\\\n", "")
        if ": " not in raw:
            continue
        inputs = set()
        for name in shlex.split(raw.split(": ", 1)[1]):
            path = Path(name)
            inputs.add((path if path.is_absolute() else repo / path).resolve())
        if repo / TARGET in inputs and repo / APP[0] in inputs:
            matches.append((depfile, inputs))
    require(len(matches) == 1, "Expected one compiler dependency file for the actual app fallback target.")
    depfile, inputs = matches[0]
    return {"depfile": str(depfile), "depfileSha256": digest(depfile),
            "inputs": {str(path): digest(path) for path in sorted(inputs)}}


def verify_pdf_sources(repo, variant, compiler, archive_dir):
    config_path = repo / ".dart_tool/package_config.json"
    config = json.loads(config_path.read_text())
    result = {"packageConfigSha256": digest(config_path), "packages": {}}
    for name, (version, archive_sha) in ARCHIVES.items():
        entry = next(package for package in config["packages"] if package["name"] == name)
        uri = urllib.parse.urljoin(config_path.as_uri(), entry["rootUri"])
        parsed = urllib.parse.urlparse(uri)
        require(parsed.scheme == "file", "Unexpected package source scheme.")
        package_root = Path(urllib.parse.unquote(parsed.path)).resolve(strict=True)
        compiled = {str(Path(path).relative_to(package_root)): checksum for path, checksum in compiler["inputs"].items()
                    if Path(path).is_relative_to(package_root)}
        require(compiled, f"The compiler did not include the actual {name} package.")
        if variant == "A":
            archive_path = archive_dir / f"{name}-{version}.tar.gz"
            if not archive_path.exists():
                url = f"https://pub.dev/api/archives/{name}-{version}.tar.gz"
                with urllib.request.urlopen(url, timeout=60) as response:
                    data = response.read(64 * 1024 * 1024 + 1)
                require(len(data) <= 64 * 1024 * 1024 and hashlib.sha256(data).hexdigest() == archive_sha,
                        "Original Pub archive differs from the locked checksum.")
                archive_path.write_bytes(data)
            require(digest(archive_path) == archive_sha, "Cached Pub archive changed.")
            expected = {}
            with tarfile.open(fileobj=io.BytesIO(archive_path.read_bytes()), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    relative = Path(member.name)
                    require(not relative.is_absolute() and ".." not in relative.parts and member.size <= 64 * 1024 * 1024,
                            "Unsafe/oversized Pub archive member.")
                    key = relative.as_posix()
                    require(key not in expected, "Duplicate Pub archive member.")
                    expected[key] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            require(all(expected.get(path) == checksum for path, checksum in compiled.items()),
                    "A compiled PDF source differs from its original locked Pub archive.")
            receipt = {"archiveSha256": archive_sha, "source": "hosted"}
        else:
            git_root = Path(capture(["git", "rev-parse", "--show-toplevel"], cwd=package_root).strip()).resolve()
            require(package_root == git_root / "packages" / name, "Unexpected Git package subdirectory.")
            require(capture(["git", "rev-parse", "HEAD"], cwd=git_root).strip() == S02_COMMIT,
                    "Resolved engine Git checkout differs from the pinned backport.")
            require(not capture(["git", "status", "--porcelain", "--untracked-files=all"], cwd=git_root).strip(),
                    "Resolved engine source checkout was modified.")
            receipt = {"source": "git", "commit": S02_COMMIT}
        result["packages"][name] = {**receipt, "root": str(package_root), "compiledInputs": compiled}
    return result


def validate_pngs(directory, report):
    rows = list(report["measuredFrames"])
    # Reference frame entries are preserved in the sidecar even though only
    # their raw RGBA hashes participate in the compact summary.
    sidecar = json.loads((directory / "frame-receipts.json").read_text())
    require(sidecar["measured"] == rows, "Frame receipt sidecar differs from the report.")
    rows += sidecar["reference"]
    for row in rows:
        require(type(row.get("captureStartedUs")) is int and type(row.get("elapsedUs")) is int
                and 0 <= row["captureStartedUs"] <= row["elapsedUs"],
                "Capture completion timestamp precedes its start.")
        relative = Path(row["pngPath"])
        path = (directory / relative).resolve(strict=True)
        require(not relative.is_absolute() and path.is_relative_to(directory), "Frame path escapes evidence.")
        require(digest(path) == row["pngSha256"], "PNG bytes differ from the captured image.")
        with Image.open(path) as image:
            rgba = image.convert("RGBA")
            require(rgba.size == (row["width"], row["height"]), "Frame dimensions differ.")
            require(rgba.getchannel("A").getextrema() == (255, 255), "Expected opaque page viewport.")
            require(hashlib.sha256(rgba.tobytes()).hexdigest() == row["rgbaSha256"], "Decoded native frame differs.")
    references = {row["inputId"]: row["rgbaSha256"] for row in sidecar["reference"]}
    require(references == report["referenceFrames"], "Reference frame identity differs.")
    return sidecar["reference"]


def validate_result(directory, report, config, owned_pid):
    require(report.get("status") == "completed" and report.get("mode") == "profile"
            and report.get("pid") == owned_pid, "Missing owned profile result.")
    for key in ("sourceCommit", "appSourcesSha256", "variant", "scenario", "route"):
        require(report.get(key) == config[key], f"Different compiled result identity: {key}")
    require(report.get("allNativeCloseFuturesCompleted") is True, "Native lifetime cleanup did not finish.")
    require(isinstance(report.get("frameworkErrors"), list), "Framework error receipt is missing.")
    reference_rows = validate_pngs(directory, report)
    expected_ids = ({"unrelated"} if config["scenario"] in ("close-pending-font", "replace-pending-font")
                    else {item["id"] for item in config["documents"]})
    require(all(set(report[name]) == expected_ids for name in ("finalProofs", "referenceProofs", "referenceFrames", "parity")),
            "Incomplete actual/reference page evidence.")
    require(len(reference_rows) == len(expected_ids) and {row["inputId"] for row in reference_rows} == expected_ids,
            "Reference frame coverage differs from the expected documents.")
    for row in reference_rows:
        item = next(item for item in config["documents"] if item["id"] == row["inputId"])
        require(row["phase"] == "reference" and row["renderState"] == "ready"
                and row["renderStateAfterCapture"] == "ready" and row["stableView"] is True
                and row["stableDocument"] is True and row["pageNumber"] == item["targetPage"],
                "Reference oracle is not the stable painted-ready target image.")
    for name in ("finalProofs", "referenceProofs"):
        for proof in report[name].values():
            require(hashlib.sha256(proof["text"].encode("utf-8")).hexdigest() == proof["textSha256"]
                    and len(proof["text"].encode("utf-16-le")) // 2 == proof["utf16CodeUnits"],
                    "Native text proof checksum/length differs.")
            require(proof["rasterWidth"] == 320 and proof["rasterHeight"] == 400
                    and type(proof["inkPixels"]) is int and 0 <= proof["inkPixels"] <= 320 * 400,
                    "Full-page raster proof is malformed.")
    for document_id in expected_ids:
        actual, reference = report["finalProofs"][document_id], report["referenceProofs"][document_id]
        input_doc = next(item for item in config["documents"] if item["id"] == document_id)
        parity = report["parity"][document_id]
        computed = {"textEqual": actual["textSha256"] == reference["textSha256"],
                    "geometryEqual": actual["geometrySha256"] == reference["geometrySha256"],
                    "fullPageRasterEqual": actual["rasterSha256"] == reference["rasterSha256"],
                    "stableMeasuredDocument": actual["stableDocument"],
                    "referenceContainsExpectedWord": input_doc["expectedWord"] in reference["text"],
                    "referenceContainsControlText": input_doc["expectedControlText"] in reference["text"],
                    "referenceHasInk": not input_doc["expectedWord"] or reference["inkPixels"] > 100}
        require(all(parity.get(key) == value for key, value in computed.items()), "A reported parity verdict differs from raw proofs.")
        samples = [row for row in report["measuredFrames"] if row["inputId"] == document_id
                   and row["pageNumber"] == input_doc["targetPage"] and row["stableDocument"] is True]
        good = [row for row in samples if row["renderState"] == "ready"
                and row["renderStateAfterCapture"] == "ready" and row["stableView"] is True
                and row["rgbaSha256"] == report["referenceFrames"][document_id]]
        first_good = good[0]["elapsedUs"] if good else None
        require(parity["firstGoodSampleUs"] == first_good
                and parity["samples"] == len(samples)
                and parity["sampledReadyFramesBeforeRecovery"] == sum(row["renderState"] == "ready"
                    and (first_good is None or row["elapsedUs"] < first_good)
                    and row["rgbaSha256"] != report["referenceFrames"][document_id] for row in samples)
                and parity["sampledMismatchingReadyFrames"] == sum(row["renderState"] == "ready"
                    and row["rgbaSha256"] != report["referenceFrames"][document_id] for row in samples)
                and parity["sampledPendingFrames"] == sum(row["renderState"] == "pending" for row in samples),
                "First-good or frame mismatch counts differ from the recorded frames.")
    inputs = {item["absolutePath"]: item for item in config["documents"]}
    resources = {path: fixture_font_resources(item) for path, item in inputs.items()}
    opens = {row["documentId"]: row for row in report["measuredEvents"] if row["type"] == "nativeOpen"}
    counts = {item["id"]: 0 for item in config["documents"]}
    for row in opens.values():
        require(row["path"] in inputs and row["pageCount"] == inputs[row["path"]]["pageCount"], "Unknown native source.")
        counts[inputs[row["path"]]["id"]] += 1
    misattributed = []
    reported_faces = set()
    request_keys = set()
    for row in report["measuredEvents"]:
        if row["type"] != "missingFonts":
            continue
        require(row["documentId"] in opens and row["eventOwnsDocument"] is True, "Event object differs from its stream owner.")
        input_doc = inputs[opens[row["documentId"]]["path"]]
        by_page_resources = resources[opens[row["documentId"]]["path"]]
        allowed_faces = {face for faces in by_page_resources.values() for face in faces}
        request_keys.update(font_query_key(query) for query in row["queries"])
        reported_faces.update(query["face"] for query in row["queries"])
        if any(query["face"] not in allowed_faces for query in row["queries"]):
            misattributed.append(row)
        if row["byPage"] is not None:
            require(all(1 <= int(page) <= input_doc["pageCount"] for page in row["byPage"]), "Invalid page attribution.")
            for page, queries in row["byPage"].items():
                require(all(font_query_key(query) in {font_query_key(q) for q in row["queries"]}
                            for query in queries), "Per-page query is absent from the operation snapshot.")
                if any(query["face"] not in by_page_resources.get(page, []) for query in queries):
                    misattributed.append(row)
    native_closed = {row["documentId"] for row in report["allEvents"] if row["type"] == "documentEventsClosed"}
    require(set(opens) <= native_closed, "A measured document event stream is still live.")
    registrations = [row for row in report["measuredEvents"] if row["type"] == "fontRegistrationStart"]
    successes = [row for row in report["measuredEvents"] if row["type"] == "fontRegistered"]
    failures = [row for row in report["measuredEvents"] if row["type"] == "fontRegistrationFailed"]
    successful_faces = {row["face"] for row in successes}
    verdict = {"nativeOpenCounts": counts, "fontRegistrationCalls": len(registrations),
               "successfulFontRegistrations": len(successes), "failedFontRegistrations": failures,
               "distinctFiveFieldRequests": [list(key) for key in sorted(request_keys)],
               "requestsWithoutObservedSuccessfulFaceRegistration": [list(key) for key in sorted(request_keys)
                    if key[0] not in successful_faces],
               "registrationScope": "Native registration records identify a face, not the manager query/context; absence is not a manager unresolved/failed result.",
               "backendFontReloads": sum(row["type"] == "backendFontReload" for row in report["measuredEvents"]),
               "misattributedEvents": len(misattributed), "parity": report["parity"]}
    if config["variant"] == "C" and config["route"] == "app":
        require(not report["frameworkErrors"] and not report["frameCaptureErrors"] and not misattributed and not failures,
                "Candidate has framework/capture errors or wrong-document reports.")
        require({item["requestedFace"] for item in config["documents"] if item["requestedFace"]} <= reported_faces,
                "A candidate fixture did not exercise its actual missing-font event.")
        require(counts["unrelated"] == 1, "Font-free peer was reopened.")
        if config["scenario"] in ("close-pending-font", "replace-pending-font"):
            require(counts["source-0"] == 1, "Closed/replaced source was reopened after font completion.")
        else:
            require(all(1 <= count <= 2 for count in counts.values()), "Candidate entered an unnecessary reopen cycle.")
        if config["scenario"] in ("late-page", "late-text"):
            require(report["lateDiscoveryObserved"] is True, "The late-discovery fixture did not exercise a native event.")
        if config["scenario"] == "shared-fonts":
            expected_faces = {face for page_map in resources.values() for faces in page_map.values() for face in faces}
            require({row["face"] for row in registrations} == expected_faces
                    and all(sum(row["face"] == face for row in registrations) == 1 for face in expected_faces)
                    and {row["face"] for row in successes} == expected_faces and not failures,
                    "Shared native font registration was duplicated, missing or failed.")
        for parity in report["parity"].values():
            require(all(parity[key] is True for key in (
                "textEqual", "geometryEqual", "fullPageRasterEqual", "stableMeasuredDocument",
                "referenceContainsExpectedWord", "referenceContainsControlText", "referenceHasInk")), "Candidate text/raster/expected-glyph parity failed.")
            require(type(parity["firstGoodSampleUs"]) is int, "No actual matching viewer frame was captured.")
    return verdict


def launch(build, config, directory):
    directory.mkdir()
    save(directory / "config.json", {**config, "output": str(directory)})
    require(bundle_receipt(build["executable"].parent) == build["bundle"], "Compiled bundle changed before launch.")
    started = time.monotonic()
    observed = None
    with (directory / "app.log").open("x") as log:
        process = subprocess.Popen([str(build["executable"])], cwd=build["root"], stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True,
                                   env={**os.environ, "PDF_5538_FONT_CONFIG": str(directory / "config.json")})
        try:
            while child_exit(process) is None:
                if time.monotonic() - started > 180:
                    raise TimeoutError("Font lifecycle process exceeded three minutes.")
                ready_path = directory / "ready.json"
                if observed is None and ready_path.exists():
                    ready = json.loads(ready_path.read_text())
                    require(ready.get("pid") == process.pid and ready.get("mode") == "profile"
                            and ready.get("sourceCommit") == config["sourceCommit"]
                            and ready.get("appSourcesSha256") == config["appSourcesSha256"], "Initial compiled receipt differs.")
                    observed = build["engine"].observe_loaded(process.pid, raw_path=directory / "engine-mappings.json")
                    observed["pdfium"] = native_mapping(process.pid, Path(config["pdfium"]), directory)
                    save(directory / "native-identity.json", observed)
                    (directory / "start").write_text("verified\n")
                time.sleep(.01)
            code = child_exit(process)
        finally:
            # child_exit uses WNOWAIT. Both group signals run while the owned
            # child is still waitable; only cleanup_group performs the reap.
            cleanup_group(process)
    require(code == 0 and observed is not None, f"App launch failed: {directory}")
    require(bundle_receipt(build["executable"].parent) == build["bundle"], "Bundle changed during the owned launch.")
    result = json.loads((directory / "result.json").read_text())
    verdict = validate_result(directory, result, config, process.pid)
    save(directory / "validated.json", verdict)
    return {"variant": config["variant"], "scenario": config["scenario"], "route": config["route"], "pid": process.pid,
            "report": str(directory / "result.json"), "reportSha256": digest(directory / "result.json"),
            "verdict": verdict}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--pdfium", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--flutter", default="flutter")
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    args = parser.parse_args()
    require(os.environ.get("CI") == "true" and platform.system() == "Linux" and platform.machine() == "x86_64"
            and os.environ.get("DISPLAY") and os.environ.get("XDG_SESSION_TYPE") == "x11", "CI Linux x64 X11 is required.")
    plan = json.loads(args.plan.read_text())
    require(set(plan) == {"A", "B", "C"} and all(re.fullmatch(r"[0-9a-f]{40}", value or "") for value in plan.values())
            and len(set(plan.values())) == 3, "Plan must contain three different immutable full source SHAs.")
    output = args.output.resolve()
    require(not output.exists() and not output.is_relative_to(ROOT) and not ROOT.is_relative_to(output), "Use a new evidence directory outside source.")
    output.mkdir(parents=True)
    pdfium = args.pdfium.resolve(strict=True)
    require(digest(pdfium) == PDFIUM_SHA256, "Different native PDFium image.")
    sdk = json.loads(capture([args.flutter, "--version", "--machine"], cwd=ROOT, timeout=120))
    require(sdk.get("frameworkRevision") == FRAMEWORK_SHA and sdk.get("engineRevision") == ENGINE_SHA
            and sdk.get("frameworkVersion") == "3.47.7" and sdk.get("dartSdkVersion", "").startswith("3.13.5"), "Different SDK.")
    fixtures = prepare(output / "fixtures")
    for case in fixtures["cases"]:
        for item in case["documents"]:
            fixture_font_resources({**item, "absolutePath": str(output / "fixtures" / item["path"])})
    archives = output / "original-pub-archives"
    archives.mkdir()
    roots, builds, receipts = {}, {}, {}
    checkout_root = Path(tempfile.mkdtemp(prefix="s02-font-checkouts-", dir=output.parent))
    report = {"schemaVersion": 1, "status": "running", "plan": plan, "sdk": sdk, "rows": [],
              "host": {"hostname": platform.node(), "platform": platform.platform(), "machine": platform.machine()},
              "workflow": {name: os.environ.get(name) for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")},
              "sampling": "one fresh warmup per source/case; ABBA for A/B and B/C on the same host; each timed datum is one fresh process",
              "limitations": ["No timing claim before reviewing sampled frame and parity receipts.",
                              "Synthetic fixtures and host; no real-book or cross-platform performance claim.",
                              "Observed non-reference ready frames disclose sampled layer-tree recovery, not OS presentation.",
                              "Capture completion timestamps are upper bounds; a final painted-ready observation is explicitly labelled."]}
    save(output / "report.json", report)
    try:
        for name, commit in plan.items():
            root = checkout_root / name
            capture(["git", "worktree", "add", "--detach", str(root), commit], cwd=ROOT)
            roots[name] = root
            receipts[name] = source(root)
        report["orchestrator"] = source(ROOT)
        validate_orchestrator(report["orchestrator"], receipts)
        report["causalPlan"] = validate_sources(receipts, roots)
        report["sources"] = receipts
        for name, root in roots.items():
            logs = output / f"build-{name}"
            logs.mkdir()
            run_logged([args.flutter, "pub", "get", "--enforce-lockfile"], logs / "resolve.log", cwd=root, timeout=900)
            require(source(root) == receipts[name], "Resolve changed pinned source or lock.")
            app_hash = json_hash({path: receipts[name]["files"][path] for path in APP})
            run_logged([args.flutter, "build", "linux", "--profile", "--no-pub", f"--target={TARGET}",
                        f"--dart-define=PDF_5538_FONT_SOURCE={plan[name]}",
                        f"--dart-define=PDF_5538_FONT_APP_SOURCES={app_hash}"], logs / "build.log", cwd=root, timeout=3600)
            executable = binary_path(root, "linux", "profile")
            compiler = compiler_receipt(root)
            save(logs / "compiler-inputs.json", compiler)
            pdf_sources = verify_pdf_sources(root, name, compiler, archives)
            save(logs / "pdf-source-identity.json", pdf_sources)
            bundle = bundle_receipt(executable.parent)
            save(logs / "bundle.json", bundle)
            require(source(root) == receipts[name], "Build changed source.")
            builds[name] = {"root": root, "executable": executable, "engine": BundledFlutterEngine(executable, "linux", "profile"),
                            "compiler": compiler, "bundle": bundle, "appHash": app_hash, "pdfSources": pdf_sources}
        report["frameworkBinaries"] = validate_common_framework(builds)
        for case in fixtures["cases"]:
            if case["id"] not in args.scenarios:
                continue
            documents = [{**item, "absolutePath": str((output / "fixtures" / item["path"]).resolve(strict=True))}
                         for item in case["documents"]]
            launches = [("warmup", name, "app") for name in ("A", "B", "C")]
            launches += [("engine-ABBA", name, "app") for name in ("A", "B", "B", "A")]
            launches += [("app-ABBA", name, "app") for name in ("B", "C", "C", "B")]
            if case["id"] in ("initial-hebrew", "late-page", "close-pending-font", "replace-pending-font"):
                launches += [("builtin-diagnostic", "B", "builtin")]
            case_dir = output / case["id"]
            case_dir.mkdir()
            for index, (block, name, route) in enumerate(launches):
                config = {"sourceCommit": plan[name], "appSourcesSha256": builds[name]["appHash"],
                          "variant": name, "scenario": case["id"], "route": route, "documents": documents,
                          "pdfium": str(pdfium), "pdfiumSha256": PDFIUM_SHA256}
                row = launch(builds[name], config, case_dir / f"{index:02}-{name}")
                row.update(block=block, warmup=block == "warmup", launchIndex=index)
                report["rows"].append(row)
                save(output / "report.json", report)
        for name, build in builds.items():
            require(source(build["root"]) == receipts[name], "Source drifted after measurement.")
            require(compiler_receipt(build["root"]) == build["compiler"], "Compiler input drifted after measurement.")
            require(verify_pdf_sources(build["root"], name, build["compiler"], archives) == build["pdfSources"],
                    "Resolved PDF package source changed after measurement.")
            require(bundle_receipt(build["executable"].parent) == build["bundle"], "Compiled payload drifted.")
        require(digest(pdfium) == PDFIUM_SHA256, "PDFium changed during measurement.")
        report["status"] = "passed"
    except BaseException as error:
        report.update(status="failed", error=repr(error))
        raise
    finally:
        save(output / "report.json", report)
        for root in roots.values():
            # Remove only checkouts created by this invocation. Evidence stays.
            capture(["git", "worktree", "remove", "--force", str(root)], cwd=ROOT)
        shutil.rmtree(checkout_root)


if __name__ == "__main__":
    main()

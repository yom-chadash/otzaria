"""Fork-only profile of app select-all/copy with an independent X11 readback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import threading
import time

from run_export_profile import (
    ROOT, PDFIUM_SHA256, FRAMEWORK_SHA, BundledFlutterEngine,
    binary_path, capture, child_exit, cleanup_group, digest, process_sample,
    run_logged, save, source_receipt,
)
from posix_engine import linux_engine_images

ENGINE_SHA = "deb287481e3ce9468f3434937ced4240a70539ca"
SELECTION_SOURCE = "lib/pdf_book/utils/pdf_text_selection_actions.dart"
DOCUMENT_IDS = ("uxlc-genesis", "mikraot-gedolot-genesis-1859", "letteris-hebrew-1910")


def corpus_document(manifest_path, document_id):
    manifest_path = manifest_path.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text())
    catalog = json.loads((ROOT / "tool/issue5538_ci/corpus_sources.json").read_text())
    if manifest.get("status") != "passed" or not isinstance(manifest.get("documents"), list):
        raise ValueError("Corpus preparation must be complete.")
    documents = manifest["documents"]
    if (not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in documents)
            or len({item["id"] for item in documents}) != len(documents)):
        raise ValueError("Malformed or duplicate corpus identity.")
    document = next((item for item in documents if item["id"] == document_id), None)
    expected = next(item for item in catalog["documents"] if item["id"] == document_id)
    if document is None or any(document.get(key) != expected[key] for key in ("sha256", "sizeBytes", "pageCount")):
        raise ValueError("The requested document differs from the pinned corpus catalog.")
    relative = Path(document["path"])
    path = (manifest_path.parent / relative).resolve(strict=True)
    if (relative.is_absolute() or not path.is_relative_to(manifest_path.parent)
            or path.suffix.lower() != ".pdf" or not path.is_file()
            or path.stat().st_size != expected["sizeBytes"] or digest(path) != expected["sha256"]):
        raise ValueError("The pinned corpus path or bytes changed.")
    return {**document, "absolutePath": str(path)}


def bundle_receipt(executable):
    return {str(path.relative_to(executable.parent)): {"sha256": digest(path), "bytes": path.stat().st_size}
            for path in sorted(executable.parent.rglob("*")) if path.is_file()}


def native_mapping(pid, pdfium, output):
    raw = Path(f"/proc/{pid}/maps").read_text()
    (output / "native-mappings.txt").write_text(raw)
    images = linux_engine_images(raw, "libpdfium.so")
    identity = pdfium.stat()
    if ({Path(item["path"]).resolve(strict=True) for item in images} != {pdfium}
            or any(item["inode"] != identity.st_ino
                   or item["device"] != (os.major(identity.st_dev), os.minor(identity.st_dev)) for item in images)
            or digest(pdfium) != PDFIUM_SHA256):
        raise ValueError("The owned process did not map the verified native PDFium image.")
    return {"canonicalPath": str(pdfium), "sha256": PDFIUM_SHA256, "mappings": images}


def clipboard_readback(xclip, expected, process_id, source_commit):
    if (expected.get("pid") != process_id or expected.get("sourceCommit") != source_commit
            or type(expected.get("utf8Bytes")) is not int or expected["utf8Bytes"] <= 0
            or type(expected.get("utf16CodeUnits")) is not int or expected["utf16CodeUnits"] <= 0):
        raise ValueError("Clipboard handshake came from a different process or an empty output.")
    command = [str(xclip), "-selection", "clipboard", "-out", "-target", "UTF8_STRING"]
    clock = time.monotonic_ns()
    completed = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    elapsed_us = (time.monotonic_ns() - clock) // 1000
    output = completed.stdout
    text = output.decode("utf-8", errors="strict")
    identity = {"utf8Bytes": len(output), "utf16CodeUnits": len(text.encode("utf-16-le")) // 2,
                "utf8Sha256": hashlib.sha256(output).hexdigest()}
    if any(expected.get(key) != value for key, value in identity.items()):
        raise ValueError("Independent X11 clipboard bytes differ from the app output string.")
    return {"status": "passed", "pid": process_id, "sourceCommit": source_commit,
            "command": command, "readbackUs": elapsed_us, **identity,
            "scope": "Independent process; hashing/decoding follows the timed xclip command."}


def sample_process(pid, directory, stop, samples, errors):
    while not stop.is_set():
        try:
            row = process_sample(pid)
            phase = directory / "phase.json"
            row["phase"] = json.loads(phase.read_text())["phase"] if phase.exists() else "opening"
            samples.append(row)
        except (FileNotFoundError, ProcessLookupError):
            break
        except Exception as error:
            errors.append(repr(error))
            break
        stop.wait(.01)


def launch(executable, engine, xclip, config, directory):
    directory.mkdir()
    config_path = directory / "config.json"
    save(config_path, {**config, "output": str(directory)})
    samples, sampling_errors = [], []
    stop = threading.Event()
    mapped, readback = None, None
    clock = time.monotonic()
    with (directory / "app.log").open("w") as log:
        process = subprocess.Popen([str(executable)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True,
                                   env={**os.environ, "PDF_5538_COPY_CONFIG": str(config_path)})
        sampler = threading.Thread(target=sample_process,
                                   args=(process.pid, directory, stop, samples, sampling_errors), daemon=True)
        sampler.start()
        try:
            while child_exit(process) is None:
                if time.monotonic() - clock > 1200:
                    raise TimeoutError("Real-document clipboard launch exceeded 20 minutes.")
                if mapped is None and (directory / "ready.json").exists():
                    ready = json.loads((directory / "ready.json").read_text())
                    if (ready.get("pid") != process.pid or ready.get("mode") != "profile"
                            or any(ready.get(key) != config[key] for key in (
                                "sourceCommit", "selectionSourceSha256", "inputSha256"))):
                        raise ValueError("Compiled app/source identity differs from the owned process.")
                    mapped = engine.observe_loaded(process.pid, raw_path=directory / "engine-mappings.json")
                    mapped["pdfium"] = native_mapping(process.pid, Path(config["pdfium"]), directory)
                    save(directory / "engine.json", mapped)
                    (directory / "start").write_text("verified\n")
                if readback is None and (directory / "clipboard-ready.json").exists():
                    if mapped is None:
                        raise ValueError("Copy ran before native engine verification.")
                    expected = json.loads((directory / "clipboard-ready.json").read_text())
                    readback = clipboard_readback(xclip, expected, process.pid, config["sourceCommit"])
                    save(directory / "clipboard-readback.json", readback)
                time.sleep(.01)
            code = child_exit(process)
        finally:
            stop.set()
            sampler.join(timeout=2)
            cleanup_group(process)
            save(directory / "host-samples.json", samples)
    engine.verify_unchanged()
    if code or mapped is None or readback is None or sampling_errors or sampler.is_alive():
        raise RuntimeError(f"Copy launch failed: exit={code}, sampling={sampling_errors}, directory={directory}")
    result = json.loads((directory / "result.json").read_text())
    if (result.get("status") != "passed" or result.get("mode") != "profile"
            or result.get("pid") != process.pid or result.get("actualNativeClipboardParity") is not True
            or result.get("flutterClipboardParity") is not True
            or any(result.get(key) != config[key] for key in (
                "sourceCommit", "selectionSourceSha256", "inputSha256", "documentId", "pageCount"))
            or any(result["text"].get(key) != readback[key] for key in ("utf8Sha256", "utf8Bytes", "utf16CodeUnits"))):
        raise ValueError("Copy completion, corpus, source or clipboard identity differs.")
    if not samples or len({row["startTicks"] for row in samples}) != 1:
        raise ValueError("Missing owned-process samples or reused PID.")
    phases = {}
    for phase in sorted({row["phase"] for row in samples} | {"select_all", "copy"}):
        rows = [row for row in samples if row["phase"] == phase]
        phases[phase] = {"samples": len(rows), "sampledPeakRssBytes": max((row["rssBytes"] for row in rows), default=None)}
    result["host"] = {"samplingIntervalMs": 10, "scope": "owned app process only",
                      "phases": phases, "wholeProcessPeakBytes": max(row["processLifetimePeakBytes"] for row in samples),
                      "independentClipboardReadback": readback}
    save(directory / "validated.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--document", choices=DOCUMENT_IDS, required=True)
    parser.add_argument("--pdfium", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--flutter", default="flutter")
    args = parser.parse_args()
    if (os.environ.get("CI") != "true" or platform.system() != "Linux" or platform.machine() != "x86_64"
            or not os.environ.get("DISPLAY") or os.environ.get("XDG_SESSION_TYPE") != "x11"):
        raise ValueError("CI=true and a Linux x64 X11 desktop are required; use run_linux_desktop.sh.")
    xclip_path = shutil.which("xclip")
    if not xclip_path:
        raise ValueError("Install xclip to verify the native clipboard independently.")
    xclip = Path(xclip_path).resolve(strict=True)
    output = args.output.resolve()
    if output.is_relative_to(ROOT) or ROOT.is_relative_to(output):
        raise ValueError("Evidence must be outside the source checkout.")
    output.mkdir(parents=True, exist_ok=False)
    source = source_receipt()
    document = corpus_document(args.corpus, args.document)
    pdfium = args.pdfium.resolve(strict=True)
    if digest(pdfium) != PDFIUM_SHA256:
        raise ValueError("The pinned native engine changed.")
    sdk = json.loads(capture([args.flutter, "--version", "--machine"], cwd=ROOT, timeout=120))
    if (sdk.get("frameworkRevision") != FRAMEWORK_SHA or sdk.get("frameworkVersion") != "3.47.7"
            or sdk.get("engineRevision") != ENGINE_SHA or not sdk.get("dartSdkVersion", "").startswith("3.13.5")):
        raise ValueError("The pinned Flutter/Dart SDK changed.")
    report = {"schemaVersion": 1, "status": "running", "source": source, "sdk": sdk, "document": document,
              "hardware": {"host": platform.node(), "machine": platform.machine(), "platform": platform.platform()},
              "workflow": {key: os.environ.get(key) for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")},
              "desktop": {key: os.environ.get(key) for key in (
                  "DISPLAY", "GDK_BACKEND", "XDG_SESSION_TYPE", "LIBGL_ALWAYS_SOFTWARE")},
              "xclip": {"path": str(xclip), "sha256": digest(xclip)}, "rows": [],
              "limitations": [
                  "One production flow with one warmup and three fresh-process observations; no before/after claim.",
                  "OS file cache is not flushed between launches.",
                  "RSS is not Dart heap allocation. Host phase peaks are sampled lower bounds; a short phase may have zero samples.",
                  "Process lifetime peak includes opening, verification copies, UTF-8 hashing and clipboard readbacks.",
                  "The full final string and native clipboard output are required by the actual copy operation.",
                  "Three observations support descriptive medians, not tail-latency or population claims."]}
    save(output / "report.json", report)
    try:
        run_logged([args.flutter, "pub", "get", "--enforce-lockfile"], output / "resolve.log", cwd=ROOT, timeout=900)
        selection_sha = digest(ROOT / SELECTION_SOURCE)
        lock_sha = digest(ROOT / "pubspec.lock")
        run_logged([args.flutter, "build", "linux", "--profile", "--no-pub",
                    "--target=tool/issue5538_ci/copy_profile_app.dart",
                    f"--dart-define=PDF_5538_COPY_SOURCE_COMMIT={source['commit']}",
                    f"--dart-define=PDF_5538_COPY_SELECTION_SHA256={selection_sha}"],
                   output / "build.log", cwd=ROOT, timeout=3600)
        if source_receipt() != source or digest(ROOT / "pubspec.lock") != lock_sha:
            raise ValueError("Source or dependency lock changed during build.")
        executable = binary_path(ROOT, "linux", "profile")
        engine = BundledFlutterEngine(executable, "linux", "profile")
        frozen_bundle = bundle_receipt(executable)
        save(output / "bundle.json", frozen_bundle)
        report.update(bundledEngine=engine.identity, bundleReceiptSha256=digest(output / "bundle.json"),
                      lockSha256=lock_sha, selectionSourceSha256=selection_sha)
        config = {"sourceCommit": source["commit"], "selectionSourceSha256": selection_sha,
                  "documentId": document["id"], "input": document["absolutePath"],
                  "inputSha256": document["sha256"], "pageCount": document["pageCount"],
                  "pdfium": str(pdfium), "pdfiumSha256": PDFIUM_SHA256}
        for index in range(4):
            row = launch(executable, engine, xclip, config, output / f"launch-{index:02}")
            row.update(launchIndex=index, warmup=index == 0)
            report["rows"].append(row)
            save(output / "report.json", report)
        if any(row["text"] != report["rows"][0]["text"] for row in report["rows"]):
            raise ValueError("Fresh processes did not produce identical clipboard text.")
        rows = report["rows"][1:]
        report["summary"] = {"warmupLaunches": 1, "measuredFreshProcesses": 3,
                             "actualNativeClipboardParity": True, "text": rows[0]["text"],
                             "medianSelectAllMs": statistics.median(row["selectAllUs"] / 1000 for row in rows),
                             "medianCopyMs": statistics.median(row["copyUs"] / 1000 for row in rows),
                             "medianFlutterReadbackMs": statistics.median(row["flutterClipboardReadbackUs"] / 1000 for row in rows),
                             "medianAfterCopyRssBytes": statistics.median(row["snapshots"]["afterCopyBeforeValidation"]["rssBytes"] for row in rows),
                             "medianAfterCloseRssBytes": statistics.median(row["afterClose"]["rssBytes"] for row in rows)}
        if (source_receipt() != source or bundle_receipt(executable) != frozen_bundle
                or digest(pdfium) != PDFIUM_SHA256 or digest(xclip) != report["xclip"]["sha256"]
                or digest(Path(document["absolutePath"])) != document["sha256"]):
            raise ValueError("Source, executable bundle, clipboard tool or input changed during measurement.")
        report["status"] = "passed"
    except BaseException as error:
        report.update(status="failed", error=repr(error))
        raise
    finally:
        save(output / "report.json", report)


if __name__ == "__main__":
    main()

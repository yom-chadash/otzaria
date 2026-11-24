"""Freeze one experimental Apple binary, then run independent ABBA/trace jobs."""

import argparse
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import shutil
import stat
import subprocess
import tarfile
import traceback

import internal_apple_run as producer
import internal_apple_trace as profiler


DOCUMENTS = ("uxlc-genesis", "mikraot-gedolot-genesis-1859", "gcc-15_2")
WORKLOADS = ("file", "custom", "lifecycle")
ORDER = ("pdfium", "coregraphics", "coregraphics", "pdfium")
MODULE = Path(__file__).resolve().parent
ROOT = MODULE.parents[1]
require, digest, save = producer.require, producer.digest, producer.write_json


def relative_name(value):
    require(isinstance(value, str) and value and not any(ord(c) < 32 for c in value)
            and "\\" not in value and ":" not in value, "Unsafe bundle member name")
    path = PurePosixPath(value)
    require(not path.is_absolute() and path.as_posix() == value
            and all(part not in (".", "..") for part in path.parts), "Unsafe bundle member path")
    return path


def link_target(member, target, bundle_name):
    require(isinstance(target, str) and target and not target.startswith("/")
            and not any(ord(c) < 32 for c in target) and "\\" not in target and ":" not in target,
            "Unsafe bundle symlink target")
    resolved = PurePosixPath(posixpath.normpath(str(PurePosixPath(member).parent / target)))
    require(resolved.parts and resolved.parts[0] == bundle_name and ".." not in resolved.parts,
            "Bundle symlink escapes its application")
    return resolved


def bundle_inventory(bundle):
    require(bundle.is_dir() and not bundle.is_symlink() and bundle.name.endswith(".app"), "Invalid application bundle")
    result = {}
    for path in [bundle, *sorted(bundle.rglob("*"))]:
        name = path.relative_to(bundle.parent).as_posix()
        relative_name(name)
        info = path.lstat()
        item = {"mode": stat.S_IMODE(info.st_mode)}
        require(item["mode"] & 0o7000 == 0, "Unexpected privileged bundle mode")
        if path.is_symlink():
            target = os.readlink(path)
            link_target(name, target, bundle.name)
            require(path.resolve(strict=True).is_relative_to(bundle.resolve()), "Bundle link resolves outside its application")
            item.update(kind="symlink", target=target)
        elif path.is_dir():
            item.update(kind="directory")
        else:
            require(path.is_file(), "Unsupported bundle member type")
            item.update(kind="file", bytes=info.st_size, sha256=digest(path))
        result[name] = item
    return result


def pack_bundle(bundle, archive):
    inventory = bundle_inventory(bundle)
    with archive.open("xb") as stream, gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as target:
            for name, item in inventory.items():
                entry = tarfile.TarInfo(name)
                entry.mode = item["mode"]
                if item["kind"] == "directory":
                    entry.type = tarfile.DIRTYPE
                    target.addfile(entry)
                elif item["kind"] == "symlink":
                    entry.type, entry.linkname = tarfile.SYMTYPE, item["target"]
                    target.addfile(entry)
                else:
                    entry.size = item["bytes"]
                    with (bundle.parent / name).open("rb") as source:
                        target.addfile(entry, source)
    require(bundle_inventory(bundle) == inventory, "Bundle changed while packaging")
    return inventory


def verify_build_inventory(inventory, bundle_name, provenance):
    regular = {name.removeprefix(bundle_name + "/"): item["sha256"]
               for name, item in inventory.items() if item["kind"] == "file"}
    require(regular == provenance["bundleFiles"], "Transport inventory differs from the original build receipt")


def unpack_bundle(archive, runtime, inventory, bundle_name):
    require(not runtime.exists(), "Runtime directory must be new")
    relative_name(bundle_name)
    require("/" not in bundle_name and bundle_name.endswith(".app")
            and inventory.get(bundle_name, {}).get("kind") == "directory", "Missing bundle root")
    for name, item in inventory.items():
        path = relative_name(name)
        require(path.parts[0] == bundle_name and item["kind"] in {"file", "directory", "symlink"}, "Foreign bundle member")
        for parent in path.parents:
            if str(parent) == ".":
                continue
            require(inventory.get(str(parent), {}).get("kind") == "directory", "Bundle member has a non-directory parent")
        if item["kind"] == "symlink":
            link_target(name, item["target"], bundle_name)
    with tarfile.open(archive, "r:gz") as source:
        entries = source.getmembers()
        require(len(entries) == len(inventory) and {e.name for e in entries} == set(inventory), "Transport membership differs")
        for entry in entries:
            item = inventory[entry.name]
            require(entry.mode == item["mode"] and not entry.islnk(), "Transport mode or hard-link differs")
            valid = ((item["kind"] == "directory" and entry.isdir()) or
                     (item["kind"] == "symlink" and entry.issym() and entry.linkname == item["target"]) or
                     (item["kind"] == "file" and entry.isfile() and entry.size == item["bytes"]))
            require(valid, "Transport member type or size differs")
        runtime.mkdir(parents=True)
        for entry in sorted(entries, key=lambda e: (len(PurePosixPath(e.name).parts), e.name)):
            item = inventory[entry.name]
            path = runtime / entry.name
            if item["kind"] == "directory":
                path.mkdir(exist_ok=True)
                path.chmod(item["mode"])
            elif item["kind"] == "file":
                with source.extractfile(entry) as incoming, path.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                path.chmod(item["mode"])
                require(digest(path) == item["sha256"], "Extracted file digest differs")
        for entry in entries:
            if inventory[entry.name]["kind"] == "symlink":
                (runtime / entry.name).symlink_to(entry.linkname)
    bundle = runtime / bundle_name
    require(bundle_inventory(bundle) == inventory, "Extracted bundle identity differs")
    return bundle


def source_identity(provenance):
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=30).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, text=True, timeout=30)
    require(head == provenance["sourceCommit"] and not dirty, "Consumer is not the frozen clean producer source")
    current = {path.name: digest(path) for path in MODULE.glob("internal_apple_*") if path.is_file()}
    require(current == provenance["harnessFiles"], "Apple harness changed after preparation")
    for name, checksum in provenance["nativeOwnerSupportFiles"].items():
        require(digest(MODULE.parent / "perf_probe" / name) == checksum, "Native process-owner helper changed")


def corpus_documents(manifest):
    content = json.loads(manifest.read_text())
    selected = [entry for entry in content["documents"] if entry["id"] in DOCUMENTS]
    require(len(selected) == len(DOCUMENTS) and {entry["id"] for entry in selected} == set(DOCUMENTS), "Incomplete Apple corpus")
    for entry in selected:
        relative = relative_name(entry["path"])
        path = manifest.parent / str(relative)
        require(not any((manifest.parent / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1))
                and path.resolve(strict=True).is_relative_to(manifest.parent.resolve()), "Corpus path escapes its directory")
        require(path.stat().st_size == entry["sizeBytes"] and digest(path) == entry["sha256"], "Corpus input changed")
    return selected


def prepare(args):
    output = args.output.resolve()
    require(not output.exists() and not output.is_relative_to(ROOT), "Preparation output must be new and external")
    output.mkdir(parents=True)
    build = output / "build"
    transport = output / "transport"
    try:
        producer.main(["--flutter", str(args.flutter), "--corpus-manifest", str(args.corpus_manifest),
                       "--output", str(build), "--prepare-only"])
        context = json.loads((build / "prepared-context.json").read_text())
        provenance = json.loads((build / "provenance.json").read_text())
        source_identity(provenance)
        require(digest(build / "provenance.json") == context["provenanceSha256"], "Prepared provenance changed")
        documents = corpus_documents(args.corpus_manifest.resolve(strict=True))
        bundle = build / str(relative_name(context["bundle"]))
        executable = bundle / str(relative_name(context["executable"]))
        require(executable.is_file() and executable.stat().st_mode & 0o111, "Prepared executable is missing")
        transport.mkdir()
        archive = transport / "bundle.tar.gz"
        inventory = pack_bundle(bundle, archive)
        verify_build_inventory(inventory, bundle.name, provenance)
        shutil.copyfile(build / "provenance.json", transport / "provenance.json")
        receipt = {"schemaVersion": 1, "phase": "prepared-binary-only", "sourceCommit": provenance["sourceCommit"],
                   "upstreamCommit": producer.UPSTREAM_SHA, "flutterRevision": producer.FLUTTER_SHA,
                   "flutterEngineRevision": producer.ENGINE_SHA, "architecture": platform.machine(),
                   "operatingSystem": platform.platform(), "macOSVersion": platform.mac_ver()[0],
                   "archiveSha256": digest(archive), "archiveBytes": archive.stat().st_size,
                   "bundleName": bundle.name, "executable": context["executable"], "executableSha256": digest(executable),
                   "bundleInventory": inventory, "provenanceSha256": digest(transport / "provenance.json"),
                   "corpusManifestSha256": digest(args.corpus_manifest), "documents": documents,
                   "expectedBaselineBlocks": [{"document": d, "workload": w} for d in DOCUMENTS for w in WORKLOADS],
                   "baselineOrder": list(ORDER), "expectedBaselineProcesses": 36, "measurementProcesses": 0,
                   "otzariaAdoptionApproved": False, "productionSdkChanged": False}
        save(transport / "transport.json", receipt)
        source_identity(provenance)
        save(output / "prepared-summary.json", {"preparedComplete": True, "transportSha256": digest(transport / "transport.json"),
            "archiveSha256": receipt["archiveSha256"], "executableSha256": receipt["executableSha256"],
            "measurementComplete": False, "measurementProcesses": 0})
    finally:
        if (build / "upstream").exists():
            shutil.rmtree(build / "upstream")


def load_prepared(args, output):
    receipt_path = args.prepared.resolve(strict=True) / "transport.json"
    require(re.fullmatch("[0-9a-f]{64}", args.expected_transport_sha256)
            and digest(receipt_path) == args.expected_transport_sha256, "Prepared transport receipt differs from the producer job output")
    receipt = json.loads(receipt_path.read_text())
    require(receipt["schemaVersion"] == 1 and receipt["phase"] == "prepared-binary-only"
            and receipt["upstreamCommit"] == producer.UPSTREAM_SHA and receipt["flutterRevision"] == producer.FLUTTER_SHA
            and receipt["flutterEngineRevision"] == producer.ENGINE_SHA, "Prepared source or experimental SDK differs")
    require(receipt["architecture"] == platform.machine() and receipt["macOSVersion"] == platform.mac_ver()[0],
            "Consumer architecture or macOS version differs from its producer")
    provenance_path = receipt_path.parent / "provenance.json"
    require(digest(provenance_path) == receipt["provenanceSha256"], "Prepared provenance digest differs")
    provenance = json.loads(provenance_path.read_text())
    source_identity(provenance)
    verify_build_inventory(receipt["bundleInventory"], receipt["bundleName"], provenance)
    require(receipt["sourceCommit"] == provenance["sourceCommit"]
            and provenance["flutter"]["frameworkVersion"] == "3.50.0"
            and provenance["flutter"]["frameworkRevision"] == producer.FLUTTER_SHA
            and provenance["flutter"]["engineRevision"] == producer.ENGINE_SHA, "Prepared SDK receipt differs")
    require(digest(args.corpus_manifest) == receipt["corpusManifestSha256"], "Consumer corpus manifest differs")
    documents = corpus_documents(args.corpus_manifest.resolve(strict=True))
    require(documents == receipt["documents"], "Consumer corpus metadata differs")
    archive = receipt_path.parent / "bundle.tar.gz"
    require(archive.stat().st_size == receipt["archiveBytes"] and digest(archive) == receipt["archiveSha256"], "Prepared binary transport changed")
    bundle = unpack_bundle(archive, output / "runtime", receipt["bundleInventory"], receipt["bundleName"])
    executable = bundle / str(relative_name(receipt["executable"]))
    require(digest(executable) == receipt["executableSha256"] and executable.stat().st_mode & 0o111, "Consumer executable differs")
    save(output / "provenance.json", provenance)
    save(output / "prepared-receipt.json", receipt)
    save(output / "consumer.json", {"sourceCommit": provenance["sourceCommit"], "transportSha256": args.expected_transport_sha256,
        "operatingSystem": platform.platform(), "architecture": platform.machine(), "macOSVersion": platform.mac_ver()[0],
        "executableSha256": digest(executable), "binarySource": "verified prepared bundle; no consumer rebuild",
        "sameHostBaselineBlock": args.phase == "baseline", "timingsPooledAcrossHosts": False})
    return receipt, provenance, documents, bundle, executable


def verify_after(receipt, provenance, bundle, manifest):
    require(bundle_inventory(bundle) == receipt["bundleInventory"], "Runtime bundle changed during workload")
    require(digest(manifest) == receipt["corpusManifestSha256"]
            and corpus_documents(manifest.resolve(strict=True)) == receipt["documents"], "Corpus changed during workload")
    source_identity(provenance)


def baseline(args, output, context):
    receipt, provenance, documents, bundle, executable = context
    document = next(item for item in documents if item["id"] == args.document)
    path = (args.corpus_manifest.parent / document["path"]).resolve()
    rows = []
    summary = {"section": "E12", "phase": "unprofiled-baseline-block", "document": args.document,
        "workload": args.workload, "runOrder": list(ORDER), "expectedProcesses": 4, "completedProcesses": 0,
        "observationsPerProcess": 20 if args.workload == "lifecycle" else 3, "runs": rows,
        "measurementComplete": False, "sameHost": True, "timingsPooledAcrossHosts": False,
        "binaryVerifiedAfterBlock": False, "transportSha256": args.expected_transport_sha256,
        "resumeUnit": {"document": args.document, "workload": args.workload},
        "resumePolicy": "Restart the complete four-process ABBA block; never join partial timings from different hosts.",
        "physicalDevice": False, "otzariaAdoptionApproved": False}
    save(output / "summary.json", summary)
    try:
        for index, backend in enumerate(ORDER, 1):
            verify_after(receipt, provenance, bundle, args.corpus_manifest)
            label = f"{args.document}-{args.workload}-{index}-{backend}"
            config = {"backend": backend, "workload": args.workload, "input": str(path),
                      "inputSha256": document["sha256"], "output": str(output / f"{label}.json")}
            config_path = output / f"{label}-config.json"
            save(config_path, config)
            save(output / "phase-progress.json", {"phase": "launching", "label": label, "completedProcesses": len(rows),
                                                  "expectedProcesses": 4, "measurementComplete": False})
            usage = producer.run(["/usr/bin/time", "-lp", executable], bundle.parent, output / f"{label}.log", timeout=600,
                                 extra_env={"ISSUE5538_CONFIG": str(config_path)})
            report = json.loads(Path(config["output"]).read_text())
            producer.validate(report, config)
            cpu = {key: float(value) for key, value in re.findall(r"(?m)^(real|user|sys)\s+([\d.]+)$", usage)}
            peak = re.search(r"(?m)^\s*(\d+)\s+maximum resident set size\s*$", usage)
            require(set(cpu) == {"real", "user", "sys"} and peak, "Missing macOS resource usage")
            rows.append({"label": label, "reportSha256": digest(config["output"]), "processSeconds": cpu,
                         "processPeakRssBytes": int(peak.group(1))})
            summary["completedProcesses"] = len(rows)
            save(output / "summary.json", summary)
            save(output / "runs.json", rows)
        verify_after(receipt, provenance, bundle, args.corpus_manifest)
        summary.update(measurementComplete=True, binaryVerifiedAfterBlock=True)
    except Exception:
        summary["error"] = traceback.format_exc()
        raise
    finally:
        save(output / "summary.json", summary)


def trace(args, output, context):
    receipt, provenance, documents, bundle, executable = context
    try:
        profiler.capture(executable=executable, app=bundle.parent, output=output, documents=documents,
            manifest=args.corpus_manifest, source_commit=provenance["sourceCommit"],
            provenance_sha256=digest(output / "provenance.json"), run=producer.run, validate=producer.validate,
            digest=digest, require=require, write_json=save, selected_documents=[args.document], selected_backends=[args.backend])
    finally:
        verify_after(receipt, provenance, bundle, args.corpus_manifest)
        save(output / "trace-binary-verification.json", {"bundleVerifiedAfterTrace": True,
            "transportSha256": args.expected_transport_sha256, "executableSha256": receipt["executableSha256"],
            "baselineTimingEvidence": False, "otzariaAdoptionApproved": False})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    phases = parser.add_subparsers(dest="phase", required=True)
    for name in ("prepare", "baseline", "trace"):
        command = phases.add_parser(name)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--corpus-manifest", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--flutter", type=Path, required=True)
        else:
            command.add_argument("--prepared", type=Path, required=True)
            command.add_argument("--expected-transport-sha256", required=True)
            command.add_argument("--document", choices=DOCUMENTS if name == "baseline" else DOCUMENTS[:2], required=True)
            if name == "baseline":
                command.add_argument("--workload", choices=WORKLOADS, required=True)
            else:
                command.add_argument("--backend", choices=("pdfium", "coregraphics"), required=True)
    args = parser.parse_args(argv)
    require(platform.system() == "Darwin" and os.environ.get("CI") == "true", "Apple jobs require macOS and CI=true")
    args.corpus_manifest = args.corpus_manifest.resolve(strict=True)
    if args.phase == "prepare":
        return prepare(args)
    output = args.output.resolve()
    require(not output.exists() and not output.is_relative_to(ROOT), "Consumer output must be new and external")
    output.mkdir(parents=True)
    try:
        context = load_prepared(args, output)
        (baseline if args.phase == "baseline" else trace)(args, output, context)
    except Exception:
        save(output / "failure.json", {"measurementComplete": False, "phase": args.phase, "error": traceback.format_exc()})
        raise
    finally:
        process_receipts = [json.loads(path.read_text()) for path in output.rglob("*.process.json")]
        if (output / "runtime").exists() and all(row.get("ownedProcessGroupReaped") is True for row in process_receipts):
            shutil.rmtree(output / "runtime")


if __name__ == "__main__":
    main()

"""Fork-only M02 work counts and native parity; debug/JIT, never latency evidence."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.parse

from PIL import Image
import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = "tool/issue5538_ci/layout_snapshot_probe_test.dart"
METRICS = "tool/issue5538_ci/layout_snapshot_metrics.dart"
HARNESS = (TARGET, METRICS, "tool/issue5538_ci/run_layout_snapshot_probe.py", "tool/issue5538_ci/layout_snapshot_host_test.py")
APP = ("lib/pdf_book/utils/pdf_book_layout.dart", "lib/pdf_book/view/pdf_book_screen.dart")
ALLOWED_APP_DELTA = set(APP) | {"test/pdf_book/book_view_layout_memo_test.dart", "pubspec.yaml", "pubspec.lock"}
PDF_PINS = {"baseline": "964b5e15e082c8633651ffab1d037631af65e156", "candidate": "cdea38083d06b445b4ec67d8345109a0457924af"}
VIEWER_DELTA = {"packages/pdfrx/lib/src/widgets/pdf_viewer.dart", "packages/pdfrx/test/pdf_viewer_test.dart",
                "packages/pdfrx/test/pdf_page_layout_snapshot_test.dart"}
INDEX = "lib/src/widgets/internals/page_layout_index.dart"
FRAMEWORK = "abaf9c523780a608bd46686fd5e53740a07077f8"
ENGINE = "deb287481e3ce9468f3434937ced4240a70539ca"
PDFIUM = "1c251efce460d67437a4c76d4be9033ae2f17bf5603f5bff1af10209b94ca723"
CORPUS = {"uxlc-genesis": ("ce28c3ed6f052b633a943d545f766be88d935c71d71b0b4c23ee172fca71af2e", 54),
          "mikraot-gedolot-genesis-1859": ("92a145512e33c4a3c8f1b1c92d2d79c4db91a49cc921a316201be81bfa85040d", 374)}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def run(command, cwd, log=None, timeout=120):
    result = subprocess.run([str(item) for item in command], cwd=cwd, capture_output=True, timeout=timeout)
    if log:
        Path(log).write_bytes(result.stdout + b"\n" + result.stderr)
    require(result.returncode == 0, f"Command failed ({result.returncode}): {command[0:3]}; log={log}")
    return result.stdout.decode()


def source(root):
    require(not run(["git", "status", "--porcelain", "--untracked-files=all"], root).strip(), "Source must be clean.")
    return {"commit": run(["git", "rev-parse", "HEAD"], root).strip(),
            "tree": run(["git", "rev-parse", "HEAD^{tree}"], root).strip(),
            "files": {name: digest(root / name) for name in (*HARNESS, *APP, "pubspec.yaml", "pubspec.lock")}}


def instrument_index(text, metrics_uri):
    require(metrics_uri.startswith("file://") and "'" not in metrics_uri, "Unsafe observer source URI.")
    replacements = [
        ("import 'dart:ui';", f"import 'dart:ui';\nimport '{metrics_uri}';"),
        ("    _root = pageRects.isEmpty", "    Issue5538LayoutCounters.indexBuilds++;\n    Issue5538LayoutCounters.indexBuildRects += pageRects.length;\n    _root = pageRects.isEmpty"),
        ("  bool matches(List<Rect> rects) {", "  bool matches(List<Rect> rects) {\n    Issue5538LayoutCounters.matchCalls++;"),
        ("      if (rects[i] != _pageRects[i]) return false;", "      Issue5538LayoutCounters.rectChecks++;\n      if (rects[i] != _pageRects[i]) return false;"),
    ]
    for before, after in replacements:
        require(text.count(before) == 1, "Pinned PageLayoutIndex instrumentation anchor changed.")
        text = text.replace(before, after, 1)
    return text


def synthetic_pdf(count):
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    for index in range(count):
        page_id = len(objects) + 1
        width, height = 400 + index % 3, 600 + index % 5
        content = b"0 0 0 rg 20 20 40 30 re f\n"
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << >> /Contents {page_id + 1} 0 R >>".encode())
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream")
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(count))
    objects[1] = f"<< /Type /Pages /Count {count} /Kids [{kids}] >>".encode()
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += f"trailer\n<< /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(output)


def documents(corpus_path, output):
    result = []
    for count in (100, 1000, 10000):
        path = output / f"synthetic-{count}.pdf"
        path.write_bytes(synthetic_pdf(count))
        result.append({"id": f"synthetic-{count}", "path": str(path), "sha256": digest(path),
                       "pageCount": count, "kind": "synthetic-native-pdf"})
    corpus = json.loads(corpus_path.read_text())
    entries = corpus["documents"]
    require(len({item["id"] for item in entries}) == len(entries), "Duplicate corpus IDs.")
    for key, (checksum, count) in CORPUS.items():
        selected = [item for item in entries if item["id"] == key]
        require(len(selected) == 1, f"Missing pinned corpus: {key}")
        item = selected[0]
        relative = Path(item["path"])
        path = (corpus_path.parent / relative).resolve(strict=True)
        require(not relative.is_absolute() and path.is_relative_to(corpus_path.parent)
                and not any((corpus_path.parent / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)),
                "Corpus path escapes or aliases the pinned directory.")
        require(item["sha256"] == checksum == digest(path) and item["pageCount"] == count
                and item["sizeBytes"] == path.stat().st_size, "Pinned corpus contents differ.")
        result.append({"id": key, "path": str(path), "sha256": checksum, "pageCount": count, "kind": "real-pdf"})
    return result


def read_packages(path):
    config = json.loads(path.read_text())
    for package in config["packages"]:
        uri = urllib.parse.urljoin(path.as_uri(), package["rootUri"])
        require(urllib.parse.urlparse(uri).scheme == "file", "Non-local package root.")
        package["rootUri"] = Path(urllib.parse.unquote(urllib.parse.urlparse(uri).path)).resolve(strict=True).as_uri() + "/"
    return config


def package_root(config, name):
    package = next(package for package in config["packages"] if package["name"] == name)
    return Path(urllib.parse.unquote(urllib.parse.urlparse(package["rootUri"]).path)).resolve(strict=True)


def dependency_receipt(config):
    result = {}
    for package in config["packages"]:
        root = package_root(config, package["name"])
        files = sorted((root / "lib").rglob("*")) if (root / "lib").is_dir() else []
        if (root / "pubspec.yaml").is_file():
            files.append(root / "pubspec.yaml")
        result[package["name"]] = {str(path.relative_to(root)): digest(path) for path in files if path.is_file()}
    return result


def prepare_overlay(root, variant, output, metrics_uri):
    original_path = (root / ".dart_tool/package_config.json").resolve(strict=True)
    config = read_packages(original_path)
    lock = yaml.safe_load((root / "pubspec.lock").read_text())
    package_receipts = {}
    for name, version in (("pdfrx", "2.13.1"), ("pdfrx_engine", "0.9.3")):
        entry = lock["packages"][name]
        require(entry["version"] == version and entry["source"] == "git"
                and entry["description"]["resolved-ref"] == PDF_PINS[variant]
                and entry["description"]["path"] == f"packages/{name}", "PDF version or compatible pin differs.")
        directory = package_root(config, name)
        git_root = Path(run(["git", "rev-parse", "--show-toplevel"], directory).strip()).resolve()
        require(directory == git_root / "packages" / name
                and run(["git", "rev-parse", "HEAD"], git_root).strip() == PDF_PINS[variant]
                and not run(["git", "status", "--porcelain", "--untracked-files=all"], git_root).strip(),
                "Resolved PDF source is not the clean pinned checkout.")
        package_receipts[name] = {"root": str(directory), "gitRoot": str(git_root), "commit": PDF_PINS[variant]}
    source_viewer = package_root(config, "pdfrx")
    copied = output / "viewer"
    copied.mkdir()
    shutil.copytree(source_viewer / "lib", copied / "lib")
    for name in ("pubspec.yaml", "LICENSE"):
        shutil.copyfile(source_viewer / name, copied / name)
    before = (copied / INDEX).read_text()
    after = instrument_index(before, metrics_uri)
    (copied / INDEX).write_text(after)
    (output / "observer.patch").write_text("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile=INDEX, tofile=INDEX)))
    overlay = json.loads(json.dumps(config))
    next(package for package in overlay["packages"] if package["name"] == "pdfrx")["rootUri"] = copied.as_uri() + "/"
    config_path = output / "packages.json"
    save(config_path, overlay)
    receipt = {"originalPackageConfigSha256": digest(original_path), "overlayPackageConfigSha256": digest(config_path),
               "originalIndexSha256": hashlib.sha256(before.encode()).hexdigest(),
               "instrumentedIndexSha256": hashlib.sha256(after.encode()).hexdigest(),
               "observerPatchSha256": digest(output / "observer.patch"), "packages": package_receipts,
               "sourceMutation": "Only private copied pdfrx/lib/src/widgets/internals/page_layout_index.dart is instrumented; application and Pub cache stay intact."}
    save(output / "overlay-receipt.json", receipt)
    return config_path, overlay, receipt


def owned_exit(process):
    status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if status is None:
        return None
    return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status


def close_owned(process):
    # Keep the leader waitable until both group signals; no PID reuse window.
    owned_exit(process)
    require(os.getpgid(process.pid) == process.pid, "Lost owned process group.")
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        if sig == signal.SIGTERM:
            time.sleep(0.1)
    process.wait(timeout=15)


def native_identity(pid, group, sdk, packages, pdfium):
    require(type(pid) is int and os.getsid(pid) == group and os.getpgid(pid) == group,
            "Reported Flutter tester is not in the owned child group.")
    proc = Path(f"/proc/{pid}")
    executable = (proc / "exe").resolve(strict=True)
    expected = sdk / "bin/cache/artifacts/engine/linux-x64/flutter_tester"
    require(executable == expected.resolve(strict=True), "Unexpected native Flutter test executable.")
    command = (proc / "cmdline").read_bytes().decode().rstrip("\0").split("\0")
    require(f"--packages={packages}" in command, "Flutter tester did not receive the private package mapping.")
    kernels = [Path(value).resolve(strict=True) for value in command if value.endswith(".dill")]
    require(len(kernels) == 1, "Missing unique compiled test kernel.")
    matches = []
    for line in (proc / "maps").read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or fields[5] != str(pdfium):
            continue
        native_stat = pdfium.stat()
        major, minor = (int(value, 16) for value in fields[3].split(":"))
        require(int(fields[4]) == native_stat.st_ino and os.makedev(major, minor) == native_stat.st_dev,
                "Mapped PDFium inode/device differs from the pinned file.")
        matches.append(line)
    require(matches and digest(pdfium) == PDFIUM, "The native process did not map the pinned PDFium.")
    return {"pid": pid, "processGroup": group, "flutterTester": str(executable), "flutterTesterSha256": digest(executable),
            "kernel": str(kernels[0]), "kernelSha256": digest(kernels[0]), "kernelBytes": kernels[0].stat().st_size,
            "packageConfigSha256": digest(packages), "pdfiumSha256": PDFIUM, "pdfiumMappings": matches,
            "command": command, "procStartTimeTicks": (proc / "stat").read_text().split(") ", 1)[1].split()[19]}


def validate_result(directory, value, config):
    require(value.get("status") == "completed" and value.get("executionMode") == "flutter-test-debug-jit"
            and value.get("instrumented") is True and value.get("latencyClaim") is False, "Wrong scope or incomplete observation.")
    for key in ("source", "appHash", "candidate"):
        require(value.get(key) == config[key], f"Compiled identity differs: {key}")
    helpers = [row for row in value["counts"] if row["kind"] == "synthetic-metadata-helper"]
    require({row["pages"] for row in helpers} == {100, 1000, 10000} and len(helpers) == 3, "Incomplete helper counts.")
    for row in helpers:
        require(row["firstBuildSizeGetters"] == row["parameterChangeSizeGetters"] == 2 * row["pages"]
                and row["cacheHits"] == 200 and row["cacheHitSizeGetters"] == (0 if config["candidate"] else 400 * row["pages"]),
                "App getter counts do not satisfy the declared workload.")
    viewers = [row for row in value["counts"] if row["kind"] == "native-viewer-stable-matrix"]
    require({(row["id"], row["phase"]) for row in viewers} == {(item["id"], phase) for item in config["documents"] for phase in ('pan', 'zoom')}
            and len(viewers) == 2 * len(config["documents"]), "Incomplete native viewer pan/zoom counts.")
    expected_tests = {f'synthetic metadata getter work {count}' for count in (100, 1000, 10000)}
    expected_tests |= {'general mutable page metadata still invalidates'} | {f"native layout {item['id']}" for item in config['documents']}
    require(set(value['completedTests']) == expected_tests and len(value['completedTests']) == len(expected_tests),
            "Completed-test checkpoints are incomplete.")
    for row in viewers:
        counts = row["counts"]
        require(row["matrixUpdates"] == 100 and type(row.get('matrixChanges')) is int and 1 <= row['matrixChanges'] <= 100
                and row["layoutCallbacks"] > 0 and counts["indexBuilds"] == counts["indexBuildRects"] == 0,
                "Stable matrix workload rebuilt its index or did no work.")
        require(counts["rectChecks"] == (0 if config["candidate"] else row["pages"] * counts["matchCalls"])
                and (counts["matchCalls"] == 0 if config["candidate"] else counts["matchCalls"] > 0),
                "Viewer rectangle validation counts differ.")
    require(value["parity"].get("generalMutablePageSemantics") is True, "Mutable metadata semantics changed.")
    for item in config["documents"]:
        states = value["parity"][item["id"]]
        require(states["mutableRectListDetected"] is True, "Mutable rectangle semantics changed.")
        for operation in ("initial", "rotate-first", "reverse", "remove-tail", "parameters"):
            require(states[operation]["pageCount"] == item["pageCount"] - (1 if operation in ("remove-tail", "parameters") else 0),
                    "Page mutation coverage differs.")
            require(all(re.fullmatch(r"[0-9a-f]{64}", states[operation][key])
                        for key in ("allRectanglesSha256", "allCenterHitsSha256")), "Missing full layout/hit hashes.")
        if item["kind"] != "real-pdf":
            continue
        proofs = list(states["allInitialPages"])
        require([row["page"] for row in proofs] == list(range(1, item["pageCount"] + 1)), "Not every real page has a native proof.")
        for key in ("rotate-first", "reverse", "remove-tail", "parameters"):
            proofs += states[key + "-affectedPages"]
        for proof in proofs:
            require(hashlib.sha256(proof["text"].encode()).hexdigest() == proof["textSha256"]
                    and len(proof["text"].encode("utf-16-le")) // 2 == proof["utf16Units"]
                    and proof["rasterWidth"] == 128 and proof["rasterHeight"] == 160
                    and all(re.fullmatch(r"[0-9a-f]{64}", proof[name]) for name in ("allCharRectanglesSha256", "rasterSha256")),
                    "Malformed native text/geometry/raster proof.")
        frame = states["initialViewport"]
        relative = Path(frame["pngPath"])
        require(not relative.is_absolute() and ".." not in relative.parts, "Viewport path escapes evidence.")
        require(digest(directory / relative) == frame["pngSha256"], "Viewport PNG hash differs.")
        with Image.open(directory / relative) as image:
            rgba = image.convert("RGBA")
            require(rgba.size == (frame["width"], frame["height"])
                    and hashlib.sha256(rgba.tobytes()).hexdigest() == frame["rgbaSha256"], "Viewport RGBA parity receipt differs.")


def launch(flutter, sdk, root, packages, target, config, directory):
    directory.mkdir()
    config = {**config, "output": str(directory)}
    save(directory / "config.json", config)
    command = [str(flutter), f"--packages={packages}", "test", "--no-pub", "--concurrency=1", "--reporter=expanded",
               f"--dart-define=M02_SOURCE={config['source']}", f"--dart-define=M02_APP_HASH={config['appHash']}",
               f"--dart-define=M02_CANDIDATE={'true' if config['candidate'] else 'false'}", str(target)]
    save(directory / "command.json", command)
    environment = {**os.environ, "CI": "true", "M02_PROBE_CONFIG": str(directory / "config.json")}
    with (directory / "test.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 1500
            while not (directory / "ready.json").is_file():
                require(owned_exit(process) is None, "Flutter test exited before its native identity handshake.")
                require(time.monotonic() < deadline, "Flutter test compile/start exceeded its bound.")
                time.sleep(0.1)
            ready = json.loads((directory / "ready.json").read_text())
            require(ready["source"] == config["source"] and ready["appHash"] == config["appHash"]
                    and ready["candidate"] is config["candidate"]
                    and ready["packageConfig"] == packages.as_uri(), "Native handshake source/packages differ.")
            identity = native_identity(ready["pid"], process.pid, sdk, packages, Path(config["pdfium"]))
            require(identity['packageConfigSha256'] == config['packageConfigSha256'], "Compiled package mapping changed.")
            save(directory / "native-identity.json", identity)
            (directory / "start").write_text("verified\n")
            deadline = time.monotonic() + 1800
            while not (directory / 'completion.json').is_file():
                require(owned_exit(process) is None, "Native test exited before its completion identity handshake.")
                require(time.monotonic() < deadline, "Native correctness/count test exceeded its bound.")
                time.sleep(0.1)
            completion = json.loads((directory / 'completion.json').read_text())
            require(completion == {key: ready[key] for key in ('pid', 'source', 'appHash')}, "Completion identity differs.")
            after = native_identity(ready['pid'], process.pid, sdk, packages, Path(config['pdfium']))
            require(after == identity, "Native tester/kernel/PDFium identity drifted before completion.")
            save(directory / 'native-identity-after.json', after)
            (directory / 'stop').write_text('verified\n')
            deadline = time.monotonic() + 60
            while owned_exit(process) is None:
                require(time.monotonic() < deadline, "Flutter test did not finish after its verified stop handshake.")
                time.sleep(0.1)
            require(owned_exit(process) == 0, "Flutter native correctness/count tests failed; inspect test.log.")
            value = json.loads((directory / "result.json").read_text())
            require(value["pid"] == ready["pid"], "Result came from another native process.")
            validate_result(directory, value, config)
            return {"path": str(directory), "resultSha256": digest(directory / "result.json"),
                    "nativeIdentity": identity, "result": value}
        finally:
            close_owned(process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--pdfium", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--flutter", type=Path, required=True)
    args = parser.parse_args()
    require(os.environ.get("CI") == "true" and platform.system() == "Linux" and platform.machine() == "x86_64", "Pinned Linux CI is required.")
    plan = json.loads(args.plan.read_text())
    require(set(plan) == set(PDF_PINS) and all(re.fullmatch(r"[0-9a-f]{40}", value) for value in plan.values())
            and len(set(plan.values())) == 2, "Use two immutable distinct source SHAs.")
    output = args.output.resolve()
    require(not output.exists() and not output.is_relative_to(ROOT) and not ROOT.is_relative_to(output), "Use a new external evidence directory.")
    output.mkdir(parents=True)
    flutter = args.flutter.resolve(strict=True)
    sdk = flutter.parent.parent
    sdk_info = json.loads(run([flutter, "--version", "--machine"], ROOT))
    require(sdk_info["frameworkVersion"] == "3.47.7" and sdk_info["frameworkRevision"] == FRAMEWORK
            and sdk_info["engineRevision"] == ENGINE and sdk_info["dartSdkVersion"].startswith("3.13.5"), "SDK changed.")
    require(run(['git', 'rev-parse', 'HEAD'], sdk).strip() == FRAMEWORK
            and not run(['git', 'diff', '--name-only', 'HEAD', '--', 'packages/flutter', 'packages/flutter_tools'], sdk).strip(),
            "SDK framework/tool source is not the pinned clean revision.")
    pdfium = args.pdfium.resolve(strict=True)
    require(digest(pdfium) == PDFIUM, "PDFium changed.")
    inputs = documents(args.corpus.resolve(strict=True), output)
    save(output / "documents.json", inputs)
    shared = output / "shared-observer"
    shared.mkdir()
    for relative in (TARGET, METRICS):
        shutil.copyfile(ROOT / relative, shared / Path(relative).name)
    observer_hashes = {path.name: digest(path) for path in shared.iterdir()}
    roots, receipts, overlays, dependencies = {}, {}, {}, {}
    checkouts = Path(tempfile.mkdtemp(prefix="m02-layout-checkouts-", dir=output.parent))
    report = {"status": "running", "scope": "instrumented Flutter test debug/JIT work counts and native correctness; no latency/frame-speed claim",
              "plan": plan, "sdk": sdk_info, "pdfiumSha256": PDFIUM, "rows": [],
              "runOrder": ["baseline", "candidate", "candidate", "baseline"], "statistics": "Deterministic work counts; repeated runs are correctness replication, not latency samples."}
    save(output / "report.json", report)
    try:
        for name, commit in plan.items():
            root = checkouts / name
            run(["git", "worktree", "add", "--detach", str(root), commit], ROOT)
            roots[name] = root
            receipts[name] = source(root)
        orchestrator = source(ROOT)
        common = {path: orchestrator["files"][path] for path in HARNESS}
        require(all({path: receipt["files"][path] for path in HARNESS} == common for receipt in receipts.values()), "Shared observer/host bytes differ.")
        changed = set(run(["git", "diff", "--name-only", plan["baseline"], plan["candidate"]], ROOT).splitlines())
        require(changed and changed <= ALLOWED_APP_DELTA, f"Unexpected product causal delta: {changed - ALLOWED_APP_DELTA}")
        report.update(orchestrator=orchestrator, sources=receipts, causalDelta=sorted(changed), sharedObserver=observer_hashes)
        locks = {name: yaml.safe_load((root / "pubspec.lock").read_text()) for name, root in roots.items()}
        require(locks['baseline']['sdks'] == locks['candidate']['sdks'], "SDK constraints changed.")
        differing = {key for key in set(locks['baseline']['packages']) | set(locks['candidate']['packages'])
                     if locks['baseline']['packages'].get(key) != locks['candidate']['packages'].get(key)}
        require(differing == {"pdfrx", "pdfrx_engine"}, "Unrelated dependencies changed.")
        for name, root in roots.items():
            destination = output / f"overlay-{name}"
            destination.mkdir()
            run([flutter, "pub", "get", "--enforce-lockfile"], root, destination / "resolve.log", timeout=900)
            require(source(root) == receipts[name], "Pub get changed measured source.")
            overlays[name] = prepare_overlay(root, name, destination, (shared / Path(METRICS).name).as_uri())
            dependencies[name] = dependency_receipt(overlays[name][1])
            save(destination / "dependency-inputs.json", dependencies[name])
        require(overlays['baseline'][2]['originalIndexSha256'] == overlays['candidate'][2]['originalIndexSha256']
                and overlays['baseline'][2]['observerPatchSha256'] == overlays['candidate'][2]['observerPatchSha256'], "Observers differ across the pair.")
        require(dependencies['baseline']['pdfrx_engine'] == dependencies['candidate']['pdfrx_engine'], "Engine runtime changed in a viewer-only comparison.")
        upstream = Path(overlays['candidate'][2]['packages']['pdfrx']['gitRoot'])
        viewer_changed = set(run(["git", "diff", "--name-only", PDF_PINS['baseline'], PDF_PINS['candidate']], upstream).splitlines())
        require(viewer_changed <= VIEWER_DELTA and viewer_changed, "Viewer pin contains unrelated changes.")
        for index, name in enumerate(report["runOrder"]):
            require(source(roots[name]) == receipts[name], "Source changed before test launch.")
            require(dependency_receipt(overlays[name][1]) == dependencies[name], "Dependency bytes changed before test launch.")
            require(digest(overlays[name][0]) == overlays[name][2]['overlayPackageConfigSha256']
                    and digest(roots[name] / '.dart_tool/package_config.json') == overlays[name][2]['originalPackageConfigSha256'],
                    "Original/private package mapping changed.")
            config = {"source": plan[name], "candidate": name == "candidate", "pdfium": str(pdfium), "documents": inputs,
                      "packageConfigSha256": overlays[name][2]['overlayPackageConfigSha256'],
                      "appHash": hashlib.sha256(json.dumps({key: receipts[name]['files'][key] for key in APP}, sort_keys=True).encode()).hexdigest()}
            require({path.name: digest(path) for path in shared.iterdir()} == observer_hashes, "Shared observer bytes changed.")
            row = launch(flutter, sdk, roots[name], overlays[name][0], shared / Path(TARGET).name, config, output / f"run-{index}-{name}")
            row.update(variant=name, index=index)
            report["rows"].append(row)
            save(output / "report.json", report)
            require(source(roots[name]) == receipts[name] and dependency_receipt(overlays[name][1]) == dependencies[name], "Source/dependencies drifted during native test.")
            require(digest(overlays[name][0]) == config['packageConfigSha256']
                    and digest(roots[name] / '.dart_tool/package_config.json') == overlays[name][2]['originalPackageConfigSha256'],
                    "Package mapping drifted during native test.")
        parities = [row['result']['parity'] for row in report['rows']]
        require(all(parity == parities[0] for parity in parities), "Native layout/text/geometry/raster/mutable-behavior parity differs across ABBA.")
        require(len({row['nativeIdentity']['flutterTesterSha256'] for row in report['rows']}) == 1 and digest(pdfium) == PDFIUM,
                "Native libraries changed during the experiment.")
        require(not run(['git', 'diff', '--name-only', 'HEAD', '--', 'packages/flutter', 'packages/flutter_tools'], sdk).strip(),
                "SDK source changed during the experiment.")
        report['status'] = 'passed'
        report['allNativeParityEqual'] = True
    except BaseException as error:
        report.update(status='failed', error=repr(error))
        raise
    finally:
        save(output / "report.json", report)
        for root in roots.values():
            run(["git", "worktree", "remove", "--force", str(root)], ROOT)
        shutil.rmtree(checkouts)


if __name__ == '__main__':
    main()

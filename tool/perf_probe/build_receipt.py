"""Immutable native build receipts shared by Windows, Linux and macOS probes.

Receipt verification is synchronous and must run outside owned application
sampling. A missing or mismatching receipt never causes a build or pub get.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys

if __package__:
    from .probe_binding import binding_identity
else:
    from probe_binding import binding_identity

FRAMEWORK_REVISION = "abaf9c523780a608bd46686fd5e53740a07077f8"
ENGINE_REVISION = "deb287481e3ce9468f3434937ced4240a70539ca"
SCHEMA = "otzaria-pdf-native-build-v1"
BUILD_ENVIRONMENT = (
    "CC", "CXX", "CFLAGS", "CXXFLAGS", "LDFLAGS", "LIBRARY_PATH", "LD_LIBRARY_PATH",
    "PKG_CONFIG_PATH", "WPE_SDK", "RUSTUP_HOME", "CARGO_HOME", "CARGO_TARGET_DIR",
    "RUSTFLAGS", "CARGO_BUILD_TARGET", "CARGO_BUILD_JOBS", "SDKROOT", "DEVELOPER_DIR",
    "MACOSX_DEPLOYMENT_TARGET", "ARCHS", "FLUTTER_BUILD_DIR", "FLUTTER_ENGINE",
    "FLUTTER_ENGINE_SRC_PATH", "LOCAL_ENGINE", "LOCAL_ENGINE_HOST", "SOURCE_DATE_EPOCH",
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_json(path):
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 64 * 1024 * 1024,
            f"Missing, symlinked or oversized build receipt: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_new(path, value):
    require(not path.exists() and not path.is_symlink(), f"Refusing to replace frozen evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        target.flush()
        os.fsync(target.fileno())


def capture(command, repository, timeout=90):
    result = subprocess.run([str(value) for value in command], cwd=repository,
                            capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=timeout)
    require(result.returncode == 0, f"Build identity command failed: {command}: {result.stderr[-3000:]}")
    return result.stdout.strip()


def file_identity(path, *, allow_empty=False):
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or (before.st_size <= 0 and not allow_empty):
            raise RuntimeError("The bundled native file is not a nonempty regular file")
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        after = os.fstat(source.fileno())
        # Compare handle metadata consistently. Windows path stat and fstat
        # can expose different file identifiers; keep the original handle
        # alive while reopening the name to detect replacement without reuse.
        with path.open("rb") as named_source:
            current = os.fstat(named_source.fileno())
    for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
        if getattr(before, field) != getattr(after, field) or getattr(after, field) != getattr(current, field):
            raise RuntimeError(f"A bundled native file changed while hashing it: {field} "
                               f"{getattr(before, field)} -> {getattr(after, field)} -> {getattr(current, field)}")
    return {"bytes": after.st_size, "sha256": digest, "mode": stat.S_IMODE(after.st_mode)}


def binary_identity(path):
    identity = file_identity(path)
    return {"bytes": identity["bytes"], "bundleSha256": identity["sha256"]}


def bundle_root(executable, system):
    executable = executable.resolve(strict=True)
    if system in ("linux", "windows"):
        return executable.parent
    if system == "macos":
        bundle = executable.parents[2]
        if (executable.parent.name != "MacOS" or executable.parent.parent.name != "Contents"
                or bundle.suffix != ".app"):
            raise RuntimeError("The tested executable is not inside a macOS app bundle")
        return bundle
    raise ValueError("Unsupported native bundle platform")


def bundle_identity(executable, system):
    """Hash the launcher, Dart AOT payload, assets and every bundled native file."""
    executable = executable.resolve(strict=True)
    bundle = bundle_root(executable, system)
    aot = bundle / {"linux": "lib/libapp.so", "windows": "data/app.so",
                    "macos": "Contents/Frameworks/App.framework/App"}[system]
    if not aot.is_file():
        raise RuntimeError(f"The built Dart AOT payload is missing: {aot}")
    aot_canonical = aot.resolve(strict=True)
    if not aot_canonical.is_relative_to(bundle):
        raise RuntimeError("The Dart AOT payload points outside the tested app bundle")
    entries = []
    for path in sorted(bundle.rglob("*"), key=lambda p: p.relative_to(bundle).as_posix()):
        relative = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            canonical = path.resolve(strict=True)
            if not canonical.is_relative_to(bundle):
                raise RuntimeError(f"A bundle link points outside the tested app: {relative}")
            entries.append({"path": relative, "kind": "symlink", "target": os.readlink(path),
                            "resolved": canonical.relative_to(bundle).as_posix()})
        elif path.is_file():
            entries.append({"path": relative, "kind": "file", **file_identity(path, allow_empty=True)})
        elif path.is_dir():
            entries.append({"path": relative, "kind": "directory", "mode": stat.S_IMODE(path.stat().st_mode)})
        else:
            raise RuntimeError(f"The app bundle contains an unsupported entry: {relative}")
    manifest = {"schemaVersion": 1, "platform": system, "bundleRoot": str(bundle),
                "executable": executable.relative_to(bundle).as_posix(),
                "aotPayload": {"path": aot.relative_to(bundle).as_posix(),
                               "resolved": aot_canonical.relative_to(bundle).as_posix(),
                               **file_identity(aot_canonical)}, "entries": entries}
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**manifest, "manifestSha256": hashlib.sha256(encoded).hexdigest()}


def verify_bundle_unchanged(executable, system, expected):
    current = bundle_identity(executable, system)
    if current != expected:
        raise RuntimeError("The app bundle or Dart AOT payload changed during the smoke")
    return current


def file_record(path):
    canonical = path.resolve(strict=True)
    record = {"path": str(path.absolute()), "canonicalPath": str(canonical),
              **file_identity(path, allow_empty=True)}
    require(path.resolve(strict=True) == canonical, f"Build input link changed while hashing: {path}")
    return record


def source_identity(repository):
    git = ["git", "-C", str(repository)]
    status = capture([*git, "status", "--porcelain=v1", "--untracked-files=all"], repository)
    require(not status, "Build reuse requires a clean source checkout")
    commit = capture([*git, "rev-parse", "HEAD"], repository)
    require(re.fullmatch(r"[0-9a-f]{40}", commit), "Invalid source commit")
    return {"repository": str(repository), "commit": commit,
            "tree": capture([*git, "rev-parse", "HEAD^{tree}"], repository), "clean": True,
            "dependencies": {name: file_record(repository / name) for name in ("pubspec.yaml", "pubspec.lock")}}


def sdk_identity(repository):
    flutter_name, dart_name = shutil.which("flutter"), shutil.which("dart")
    require(flutter_name and dart_name, "Build reuse requires identified Flutter and Dart executables")
    flutter, dart_wrapper = Path(flutter_name).resolve(strict=True), Path(dart_name).resolve(strict=True)
    root = flutter.parent.parent
    dart = root / "bin/cache/dart-sdk/bin" / ("dart.exe" if os.name == "nt" else "dart")
    sdk_wrapper = root / "bin" / ("dart.bat" if os.name == "nt" else "dart")
    require(dart_wrapper in {dart.resolve(strict=True), sdk_wrapper.resolve(strict=True)},
            "Flutter and the selected Dart command come from different SDKs")
    sdk = json.loads(capture([flutter, "--version", "--machine"], repository))
    identity = {key: sdk[key] for key in ("frameworkVersion", "frameworkRevision", "engineRevision", "dartSdkVersion")}
    require(identity["frameworkVersion"] == "3.47.7" and identity["frameworkRevision"] == FRAMEWORK_REVISION
            and identity["engineRevision"] == ENGINE_REVISION and re.match(r"^3\.13\.5(?:\s|$)", identity["dartSdkVersion"]),
            "Build receipt requires the exact pinned Flutter/Dart/engine SDK")
    tools = [flutter, dart_wrapper, dart, root / "bin/cache/flutter_tools.snapshot", root / "bin/internal/engine.version"]
    require(all(path.is_file() for path in tools), "Missing SDK compiler/wrapper identity")
    return {"root": str(root), "identity": identity,
            "tools": [file_record(path) for path in tools]}


def specification(repository, system, mode, binding_mode, expected_engine_sha256=""):
    repository = Path(repository).resolve(strict=True)
    expected_system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system())
    require(system == expected_system, "Build receipt platform differs from this host")
    require(mode in ("profile", "release") and binding_mode == "framework",
            "Native PDF reuse requires the framework target in profile or release mode")
    if expected_engine_sha256:
        require(system == "windows" and mode == "release" and re.fullmatch(r"[0-9a-f]{64}", expected_engine_sha256),
                "Expected patched engine hash is valid only for Windows release")
    if system == "windows" and mode == "release":
        require(bool(expected_engine_sha256), "Windows release reuse requires the exact patched Flutter engine SHA")
    return {"repository": str(repository), "platform": system, "mode": mode,
            "architecture": platform.machine(), "binding": binding_identity(binding_mode),
            "expectedPatchedWindowsEngineSha256": expected_engine_sha256,
            "buildEnvironment": {key: os.environ.get(key) for key in BUILD_ENVIRONMENT}}


def build_inputs(spec):
    repository = Path(spec["repository"])
    sdk = sdk_identity(repository)
    command = {"executable": sdk["tools"][0]["canonicalPath"],
               "arguments": ["build", spec["platform"], f"--{spec['mode']}", "--no-pub",
                             f"--target={spec['binding']['probeTarget']}", "--dart-define=SENTRY_DSN=",
                             "--dart-define=INTEGRATION_TEST_SHOULD_REPORT_RESULTS_TO_NATIVE=false"]}
    config = repository / ".dart_tool/package_config.json"
    graph = repository / ".dart_tool/package_graph.json"
    require(config.is_file() and graph.is_file() and not config.is_symlink() and not graph.is_symlink(),
            "Missing root package resolution; reuse never runs pub get")
    for nested in (repository / "tool", repository / "tool/perf_probe"):
        require(not (nested / ".dart_tool/package_config.json").exists(), "Nested package resolution is not accepted")
    return {"specification": spec, "source": source_identity(repository), "sdk": sdk,
            "command": command, "packageResolution": {"config": file_record(config), "graph": file_record(graph)}}


def depfile_paths(raw):
    """Decode Flutter depfiles, including doubled Windows separators and spaces."""
    tokens, token, index = [], [], 0
    while index < len(raw):
        value = raw[index]
        if value == "\\":
            index += 1
            require(index < len(raw), "Trailing depfile escape")
            if raw[index] not in "\r\n":
                token.append(raw[index])
        elif value.isspace():
            if token:
                tokens.append("".join(token))
                token.clear()
        else:
            token.append(value)
        index += 1
    if token:
        tokens.append("".join(token))
    return tokens


def compiler_inputs(repository, target):
    repository = Path(repository)
    matches = []
    required = {(repository / target).resolve(strict=True),
                (repository / "integration_test/support/perf_resource_workload.dart").resolve(strict=True)}
    for depfile in (repository / ".dart_tool/flutter_build").glob("*/kernel_snapshot_program.d"):
        text = depfile.read_text(encoding="utf-8")
        sides = text.split(": ")
        require(len(sides) == 2, f"Invalid compiler depfile: {depfile}")
        paths = {Path(name) if Path(name).is_absolute() else repository / name
                 for name in depfile_paths(sides[1])}
        canonical = {path.resolve(strict=True) for path in paths}
        if required <= canonical:
            matches.append((depfile, paths))
    require(len(matches) == 1, "Need exactly one compiler depfile for the measured framework workload")
    depfile, paths = matches[0]
    return {"depfile": file_record(depfile),
            "inputs": [file_record(path) for path in sorted(paths, key=str)]}


def compiler_tools(sdk):
    engine = Path(sdk["root"]) / "bin/cache/artifacts/engine"
    prefix = {"Linux": "linux-", "Darwin": "darwin-", "Windows": "windows-"}.get(platform.system())
    tools = [path for path in engine.rglob("gen_snapshot*")
             if path.is_file() and path.relative_to(engine).parts[0].startswith(prefix)]
    require(tools, "Missing AOT gen_snapshot compiler identity")
    return [file_record(path) for path in sorted(tools, key=str)]


def expected_executable(repository, system, mode):
    repository = Path(repository)
    if system == "windows":
        return (repository / f"build/windows/x64/runner/{mode.title()}/otzaria.exe").resolve(strict=True)
    if system == "linux":
        architecture = {"x86_64": "x64", "AMD64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
        require(architecture is not None, "Unsupported native Linux build architecture")
        return (repository / f"build/linux/{architecture}/{mode}/bundle/otzaria").resolve(strict=True)
    import plistlib
    apps = list((repository / f"build/macos/Build/Products/{mode.title()}").glob("*.app"))
    require(len(apps) == 1, "Expected exactly one native macOS app bundle")
    with (apps[0] / "Contents/Info.plist").open("rb") as source:
        name = plistlib.load(source).get("CFBundleExecutable")
    require(isinstance(name, str) and name not in ("", ".", "..") and Path(name).name == name, "Invalid macOS bundle executable")
    return (apps[0] / "Contents/MacOS" / name).resolve(strict=True)


def build_output(spec, executable, sdk):
    executable = Path(executable).resolve(strict=True)
    require(executable == expected_executable(spec["repository"], spec["platform"], spec["mode"]),
            "The reused executable is not the requested source/mode build output")
    bundle = bundle_identity(executable, spec["platform"])
    if spec["expectedPatchedWindowsEngineSha256"]:
        engine = file_identity(executable.parent / "flutter_windows.dll")
        require(engine["sha256"] == spec["expectedPatchedWindowsEngineSha256"], "Patched Windows engine hash differs")
    return {"executable": str(executable), "bundle": bundle,
            "compilerInputs": compiler_inputs(spec["repository"], spec["binding"]["probeTarget"]),
            "aotCompilers": compiler_tools(sdk)}


def prepare(spec, receipt):
    require(not receipt.exists() and not receipt.is_symlink(), "Create refuses an existing or symlinked build receipt")
    return {"schemaVersion": SCHEMA, "kind": "pre-build-inputs", "inputs": build_inputs(spec)}


def freeze(spec, receipt, before, executable):
    require(not receipt.exists() and not receipt.is_symlink(), "Create refuses an existing or symlinked build receipt")
    require(before.get("schemaVersion") == SCHEMA and before.get("kind") == "pre-build-inputs", "Invalid pre-build receipt")
    current = build_inputs(spec)
    require(current == before["inputs"], "Source, SDK, dependencies, flags or target changed while building")
    output = build_output(spec, executable, current["sdk"])
    require(build_inputs(spec) == current, "Build inputs changed while freezing the native output")
    value = {"schemaVersion": SCHEMA, "kind": "frozen-native-build", "status": "frozen",
             "createdAtUtc": datetime.now(timezone.utc).isoformat(), "inputs": current, "output": output,
             "scope": "Build and all identity verification are outside application resource sampling; runtime case/output/data are not compiler inputs."}
    write_new(receipt, value)
    return file_identity(receipt)["sha256"]


def verify(spec, receipt, expected_sha256, executable):
    require(isinstance(expected_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_sha256), "Reuse requires the pinned receipt SHA-256")
    require(not receipt.is_symlink(), "Cannot reuse a symlinked build receipt")
    identity = file_identity(receipt)
    require(identity["sha256"] == expected_sha256, "Frozen build receipt bytes changed")
    value = read_json(receipt)
    require(value.get("schemaVersion") == SCHEMA and value.get("kind") == "frozen-native-build" and value.get("status") == "frozen", "Incomplete native build receipt")
    current = build_inputs(spec)
    require(current == value["inputs"], "Frozen source, SDK, dependencies, flags or target no longer match; rebuild is forbidden")
    actual = build_output(spec, executable, current["sdk"])
    require(actual == value["output"], "AOT, native binary, assets, links, compiler or compiler inputs changed; rebuild is forbidden")
    require(file_identity(receipt) == identity and source_identity(Path(spec["repository"])) == current["source"],
            "Receipt or source changed while verifying the frozen build")
    return {"schemaVersion": SCHEMA, "status": "passed", "receiptSha256": expected_sha256,
            "sourceCommit": current["source"]["commit"], "sourceTree": current["source"]["tree"],
            "bundleManifestSha256": actual["bundle"]["manifestSha256"],
            "aotPayload": actual["bundle"]["aotPayload"], "verificationOverlapsAppSampling": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "create", "verify"))
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--platform", choices=("linux", "macos", "windows"), required=True)
    parser.add_argument("--mode", choices=("profile", "release"), required=True)
    parser.add_argument("--binding-mode", choices=("framework",), default="framework")
    parser.add_argument("--expected-engine-sha256", default="")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--receipt-sha256")
    parser.add_argument("--before", type=Path)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = specification(args.repository, args.platform, args.mode, args.binding_mode, args.expected_engine_sha256)
    receipt = args.receipt.absolute()
    if args.action == "prepare":
        result = prepare(spec, receipt)
    elif args.action == "create":
        require(args.before and args.executable, "Create requires pre-build inputs and native executable")
        receipt_sha = freeze(spec, receipt, read_json(args.before), args.executable)
        result = verify(spec, receipt, receipt_sha, args.executable)
    else:
        require(args.executable is not None, "Verify requires the native executable")
        result = verify(spec, receipt, args.receipt_sha256, args.executable)
    write_new(args.output.absolute(), result)
    print(json.dumps({"action": args.action, "status": "passed", "output": str(args.output),
                      "receiptSha256": result.get("receiptSha256")}))


if __name__ == "__main__":
    main()



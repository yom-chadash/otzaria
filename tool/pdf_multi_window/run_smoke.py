#!/usr/bin/env python3
"""Build and verify the actual two-window PDF correctness scenario.

This runner records source, engine and pass/fail evidence only. It does not
sample resources, time actions or compare performance.
"""

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time

try:
    from .engine import BundledFlutterEngine, binary_identity, bundle_identity, verify_bundle_unchanged
except ImportError:
    from engine import BundledFlutterEngine, binary_identity, bundle_identity, verify_bundle_unchanged


ROOT = Path(__file__).resolve().parents[2]
TARGET = "integration_test/pdf_multi_window_smoke.dart"
SHA = re.compile(r"[a-f0-9]{40}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def interrupt(signum, _frame):
    raise RuntimeError(f"The owned smoke was interrupted by signal {signum}")


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeError(f"Unexpectedly large correctness receipt: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def capture(command, *, cwd=ROOT):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    if result.returncode:
        raise RuntimeError(f"Command failed: {command!r}: {result.stderr.strip()}")
    return result.stdout.strip()


def source_snapshot(expected=None, *, root=ROOT):
    commit = capture(["git", "rev-parse", "HEAD"], cwd=root)
    status = capture(["git", "-c", "core.quotepath=false", "status", "--porcelain=v1",
                      "--untracked-files=all"], cwd=root)
    if not SHA.fullmatch(commit) or (expected is not None and commit != expected):
        raise RuntimeError("The source commit changed during the smoke")
    if status:
        raise RuntimeError(f"The smoke requires an immutable clean source tree: {status}")
    return {"commit": commit, "clean": True,
            "pubspecSha256": file_sha(root / "pubspec.yaml"),
            "lockSha256": file_sha(root / "pubspec.lock"),
            "targetSha256": file_sha(root / TARGET)}


def child_exit_without_reaping(process):
    # Retaining the waitable child prevents PID/PGID reuse until group cleanup.
    if process.returncode is not None:
        raise RuntimeError("The owned POSIX child was already reaped; refusing process-group signals")
    try:
        result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError as error:
        raise RuntimeError("The owned POSIX child is no longer waitable; refusing process-group signals") from error
    if result is None:
        return None
    return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status


def darwin_group_has_only_zombies(process):
    if sys.platform != "darwin":
        return False
    # XNU can refuse killpg for an exiting zombie group. Require the unreaped
    # child to be visible and every group member to be a zombie before accepting.
    deadline = time.monotonic() + 5
    while child_exit_without_reaping(process) is None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,stat="], capture_output=True,
                            text=True, encoding="utf-8", env={**os.environ, "LC_ALL": "C"}, timeout=5)
    if result.returncode:
        return False
    members = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 4 or not all(field.isdigit() for field in fields[:3]):
            return False
        pid, parent, group = map(int, fields[:3])
        if group == process.pid:
            members.append((pid, parent, fields[3]))
    leader = [member for member in members if member[0] == process.pid]
    return (len(leader) == 1 and leader[0][1] == os.getpid()
            and all(state.startswith("Z") for _, _, state in members))


class OwnedProcess:
    """Only terminate our child handle or the POSIX session we created."""

    def __init__(self, command, *, log, cwd=ROOT, environment=None):
        self.job = None
        self.closed = False
        self.process = subprocess.Popen(command, cwd=cwd, env=environment,
                                        stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=os.name != "nt")
        if os.name == "nt":
            try:
                self._own_windows_job()
            except BaseException:
                self.process.terminate()
                self.process.wait(timeout=10)
                raise

    def _own_windows_job(self):
        import ctypes
        from ctypes import wintypes

        class Limits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Limits), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(self.process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            kernel.CloseHandle(job)
            raise
        self.job = (kernel, job)

    def poll(self):
        if self.closed or os.name == "nt":
            return self.process.poll()
        return child_exit_without_reaping(self.process)

    def wait(self, timeout):
        if self.closed or os.name == "nt":
            return self.process.wait(timeout=timeout)
        deadline = time.monotonic() + timeout
        while (code := self.poll()) is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.process.args, timeout)
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        return code

    def close(self):
        if self.closed:
            return
        if os.name != "nt":
            for signum in (signal.SIGTERM, signal.SIGKILL):
                # No Popen.poll()/wait() may reap the leader before these signals.
                child_exit_without_reaping(self.process)
                try:
                    os.killpg(self.process.pid, signum)
                except ProcessLookupError:
                    pass
                except PermissionError as error:
                    if error.errno != errno.EPERM or not darwin_group_has_only_zombies(self.process):
                        raise
                if signum == signal.SIGTERM:
                    time.sleep(0.2)
            # This is the only POSIX reap. No group signal is allowed after it.
            self.process.wait(timeout=10)
            self.closed = True
            return
        if self.job is not None:
            kernel, job = self.job
            kernel.CloseHandle(job)
            self.job = None
        elif self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self.closed = True


def run_logged(command, path, *, timeout, cwd=ROOT, environment=None):
    with path.open("w", encoding="utf-8") as log:
        child = OwnedProcess(command, log=log, cwd=cwd, environment=environment)
        try:
            code = child.wait(timeout=timeout)
            if code:
                raise RuntimeError(f"Command failed ({code}); inspect {path}")
        finally:
            child.close()


def binary_path(system, mode, *, root=ROOT):
    if system == "windows":
        executable = root / f"build/windows/x64/runner/{mode.title()}/otzaria.exe"
    elif system == "linux":
        architecture = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine())
        if architecture is None:
            raise RuntimeError(f"Unsupported Linux architecture: {platform.machine()}")
        executable = root / f"build/linux/{architecture}/{mode}/bundle/otzaria"
    else:
        products = root / "build/macos/Build/Products" / mode.title()
        apps = list(products.glob("*.app"))
        if len(apps) != 1:
            raise RuntimeError(f"Expected one freshly built macOS app in {products}")
        with (apps[0] / "Contents/Info.plist").open("rb") as source:
            name = plistlib.load(source).get("CFBundleExecutable")
        if not isinstance(name, str) or not name or Path(name).name != name or name in (".", ".."):
            raise RuntimeError("Invalid CFBundleExecutable")
        executable = apps[0] / "Contents/MacOS" / name
    if not executable.is_file() or (os.name != "nt" and not os.access(executable, os.X_OK)):
        raise RuntimeError(f"Built executable is missing or not executable: {executable}")
    return executable.resolve(strict=True)


def engine_identity(executable, system, mode, revision, expected=None):
    if system != "windows":
        return BundledFlutterEngine(executable, system, mode).identity
    digest = binary_identity(executable.parent / "flutter_windows.dll")["bundleSha256"]
    if expected and digest != expected:
        raise RuntimeError("The bundled Windows engine does not match the patched action hash")
    if mode == "release" and expected is None:
        raise RuntimeError("Windows release requires the patched engine's verified SHA-256")
    identity = {"schemaVersion": 1, "platform": system, "architecture": "x64",
                "buildMode": mode, "engineRevision": revision, "bundleSha256": digest,
                "verification": "patched-action-sha256" if expected else "bundle-sha256"}
    if expected:
        identity["expectedSha256"] = expected
    return identity


def validate_completion(directory, receipt):
    completion = read_json(directory / "completion.json")
    required = {"schemaVersion": 1, "status": "passed", "sourceSha": receipt["sourceSha"],
                "buildMode": receipt["buildMode"], "bindingClass": "WidgetsFlutterBinding",
                "nativePlatformDispatcher": True, "engineIdentity": receipt["engineIdentity"],
                "engineRevision": receipt["engineRevision"], "unhandledErrors": [], "error": None}
    for name, expected in required.items():
        if completion.get(name) != expected or type(completion.get(name)) is not type(expected):
            raise RuntimeError(f"Completion evidence does not match the requested smoke: {name}")
    if completion.get("reportSha256") != file_sha(directory / "report.json"):
        raise RuntimeError("The completed correctness report has changed")
    report = read_json(directory / "report.json")
    if not isinstance(report, dict):
        raise RuntimeError("The correctness report must be an object")
    return completion


def run_app(output, receipt):
    executable = Path(receipt["executable"])
    environment = dict(os.environ, CI="true", PDF_MULTI_WINDOW_SOURCE=receipt["sourceSha"],
                       PDF_MULTI_WINDOW_ENGINE_JSON=json.dumps(receipt["engineIdentity"], separators=(",", ":")),
                       PDF_MULTI_WINDOW_OUTPUT=str(output / "run"), PDF_MULTI_WINDOW_DATA=str(output / "data"))
    if file_sha(executable) != receipt["executableSha256"]:
        raise RuntimeError("The smoke executable changed before launch")
    identity = engine_identity(executable, receipt["platform"], receipt["buildMode"],
                               receipt["engineRevision"], receipt.get("expectedEngineSha256"))
    if identity != receipt["engineIdentity"]:
        raise RuntimeError("The bundled engine changed before launch")
    verify_bundle_unchanged(executable, receipt["platform"], receipt["bundleIdentity"])
    with (output / "app.log").open("w", encoding="utf-8") as log:
        child = OwnedProcess([str(executable)], cwd=executable.parent, log=log, environment=environment)
        try:
            deadline = time.monotonic() + 600
            completion_path = output / "run/completion.json"
            while not completion_path.exists():
                if (code := child.poll()) is not None:
                    raise RuntimeError(f"The native app exited before completion: {code}")
                if time.monotonic() >= deadline:
                    raise RuntimeError("The native correctness scenario did not complete within 600 seconds")
                time.sleep(0.1)
            completion = validate_completion(output / "run", receipt)
            if child.poll() is not None:
                raise RuntimeError("The app exited before the host verified its evidence")
            if receipt["platform"] != "windows":
                engine = BundledFlutterEngine(executable, receipt["platform"], receipt["buildMode"])
                mapped = engine.observe_loaded(child.process.pid, raw_path=output / "engine-mapping-raw.json")
                mapped.pop("rawObservation", None)
                write_json(output / "engine-mapping.json", mapped)
            (output / "run/stop").write_text(receipt["sourceSha"] + "\n", encoding="ascii")
            code = child.wait(timeout=60)
            if code:
                raise RuntimeError(f"The completed app did not exit successfully: {code}")
        finally:
            child.close()
    validate_completion(output / "run", receipt)
    current = engine_identity(executable, receipt["platform"], receipt["buildMode"],
                              receipt["engineRevision"], receipt.get("expectedEngineSha256"))
    if current != identity or file_sha(executable) != receipt["executableSha256"]:
        raise RuntimeError("The native binaries changed during the smoke")
    bundle = verify_bundle_unchanged(executable, receipt["platform"], receipt["bundleIdentity"])
    write_json(output / "verified.json", {"status": "passed", "pid": child.process.pid,
               "sourceSha": receipt["sourceSha"], "reportSha256": completion["reportSha256"],
               "engineIdentity": identity, "bundleManifestSha256": bundle["manifestSha256"],
               "aotPayload": bundle["aotPayload"], "exitCode": code})


def linux_session(output, receipt):
    with (output / "openbox.log").open("w", encoding="utf-8") as log:
        manager = OwnedProcess(["openbox", "--sm-disable"], log=log)
        try:
            deadline = time.monotonic() + 20
            while True:
                result = subprocess.run(["wmctrl", "-m"], capture_output=True, text=True, timeout=2)
                if result.returncode == 0 and "Name: Openbox" in result.stdout:
                    break
                if manager.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Openbox did not acquire the owned X11 display")
                time.sleep(0.1)
            session = {"windowManager": result.stdout, "display": capture(["xdpyinfo"]),
                       "windowManagerProperty": capture(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"]),
                       "dbus": capture(["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
                                        "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames"])}
            write_json(output / "session.json", session)
            run_app(output, receipt)
            if manager.poll() is not None:
                raise RuntimeError("The owned window manager exited during the smoke")
        finally:
            manager.close()


def run(args):
    output = args.output.resolve()
    if args.inside_session:
        linux_session(output, read_json(output / "host.json"))
        return
    output.mkdir(parents=True, exist_ok=False)
    for name in ("run", "data"):
        (output / name).mkdir()
    receipt = {"schemaVersion": 1, "status": "preparing", "platform": args.platform,
               "buildMode": args.mode, "scenario": "two-native-pdf-windows-correctness"}
    try:
        before = source_snapshot()
        receipt.update(sourceSha=before["commit"], source={"beforeBuild": before})
        flutter = shutil.which(args.flutter)
        if flutter is None:
            raise RuntimeError("Flutter is not available on PATH")
        version = capture([flutter, "--version", "--machine"])
        sdk = json.loads(version[version.index("{"):])
        if sdk.get("frameworkVersion") != "3.47.7" or not SHA.fullmatch(str(sdk.get("engineRevision", ""))):
            raise RuntimeError("The smoke requires the app's locked Flutter 3.47.7 SDK")
        receipt.update(flutter=sdk, engineRevision=sdk["engineRevision"])
        write_json(output / "host.json", receipt)
        command = [flutter, "build", args.platform, "--" + args.mode, "--no-pub", "--target=" + TARGET,
                   "--dart-define=PDF_MULTI_WINDOW_SOURCE=" + before["commit"],
                   "--dart-define=PDF_MULTI_WINDOW_ENGINE_REVISION=" + sdk["engineRevision"]]
        run_logged(command, output / "build.log", timeout=2400, environment=dict(os.environ, CI="true"))
        after = source_snapshot(before["commit"])
        if before != after:
            raise RuntimeError("Source files changed during the build")
        receipt["source"]["afterBuild"] = after
        executable = binary_path(args.platform, args.mode)
        identity = engine_identity(executable, args.platform, args.mode, sdk["engineRevision"],
                                   args.expected_engine_sha256)
        receipt.update(status="running", executable=str(executable), executableSha256=file_sha(executable),
                       engineIdentity=identity, expectedEngineSha256=args.expected_engine_sha256,
                       bundleIdentity=bundle_identity(executable, args.platform))
        write_json(output / "host.json", receipt)
        if args.platform == "linux":
            runtime = output / "xdg-runtime"
            runtime.mkdir(mode=0o700)
            environment = dict(os.environ, CI="true", XDG_RUNTIME_DIR=str(runtime),
                               XDG_SESSION_TYPE="x11", GDK_BACKEND="x11")
            command = ["xvfb-run", "--auto-servernum", f"--error-file={output / 'xvfb.log'}",
                       "--server-args=-screen 0 1440x1000x24 -dpi 96 -nolisten tcp",
                       "dbus-run-session", "--", sys.executable, str(Path(__file__).resolve()),
                       "--platform", "linux", "--mode", args.mode, "--output", str(output), "--inside-session"]
            run_logged(command, output / "session.log", timeout=720, environment=environment)
        else:
            run_app(output, receipt)
        final = source_snapshot(before["commit"])
        if final != before:
            raise RuntimeError("Source files changed during the native run")
        receipt["source"]["afterRun"] = final
        receipt["status"] = "passed"
    except BaseException as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        write_json(output / "host.json", receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("linux", "macos", "windows"))
    parser.add_argument("--mode", required=True, choices=("profile", "release"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--flutter", default="flutter")
    parser.add_argument("--expected-engine-sha256")
    parser.add_argument("--inside-session", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    actual = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system())
    if args.platform != actual:
        parser.error("The requested platform must match the native host")
    if not args.output.is_absolute() or args.output.is_symlink() or args.output.resolve().is_relative_to(ROOT):
        parser.error("--output must be a fresh absolute directory outside the source tree")
    if args.inside_session:
        if args.platform != "linux" or not (args.output / "host.json").is_file():
            parser.error("The internal session requires an existing owned Linux receipt")
    elif args.output.exists():
        parser.error("Existing evidence is never reused or overwritten")
    if args.expected_engine_sha256 is not None and (
            args.platform != "windows" or args.mode != "release" or
            not DIGEST.fullmatch(args.expected_engine_sha256)):
        parser.error("A lowercase expected engine SHA-256 is supported only for Windows release")
    if args.platform == "windows" and args.mode == "release" and args.expected_engine_sha256 is None:
        parser.error("Windows release requires --expected-engine-sha256 from the patched engine action")
    os.environ["CI"] = "true"
    signal.signal(signal.SIGTERM, interrupt)
    run(args)


if __name__ == "__main__":
    main()

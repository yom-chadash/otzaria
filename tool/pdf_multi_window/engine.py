"""Identify the bundled Flutter engine and the image mapped by an owned process."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess


def file_identity(path, *, allow_empty=False):
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or (before.st_size <= 0 and not allow_empty):
            raise RuntimeError("The bundled native file is not a nonempty regular file")
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        after = os.fstat(source.fileno())
    current = path.stat()
    for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
        if getattr(before, field) != getattr(after, field) or getattr(after, field) != getattr(current, field):
            raise RuntimeError("A bundled native file changed while hashing it")
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


def linux_engine_images(source, name):
    images = []
    for line in source.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6:
            continue
        path = fields[5]
        deleted = path.endswith(" (deleted)")
        if PurePosixPath(path.removesuffix(" (deleted)")).name != name:
            continue
        if deleted or not PurePosixPath(path).is_absolute():
            raise RuntimeError("The mapped Flutter engine was deleted or has no absolute path")
        device = fields[3].split(":")
        if len(device) != 2:
            raise RuntimeError("The mapped Flutter engine has no device identity")
        images.append({"path": path, "inode": int(fields[4]),
                       "device": tuple(int(part, 16) for part in device)})
    return images


def darwin_engine_images(source, pid, name):
    # -F0 separates fields with NUL; newline separates process/file records.
    process_seen, descriptor, paths = False, None, []
    for raw in source.split("\0"):
        field = raw.removeprefix("\n")
        if not field:
            continue
        kind, value = field[0], field[1:]
        if kind == "p":
            if process_seen or value != str(pid):
                raise RuntimeError("The mapped-image observation belongs to another process")
            process_seen, descriptor = True, None
        elif kind == "f":
            descriptor = value
        elif kind == "n":
            if not process_seen or descriptor != "txt":
                raise RuntimeError("The mapped-image observation is not a process text mapping")
            if PurePosixPath(value).name == name:
                if not PurePosixPath(value).is_absolute():
                    raise RuntimeError("The mapped Flutter engine has no absolute path")
                paths.append({"path": value})
    if not process_seen:
        raise RuntimeError("Missing process identity in the mapped-image observation")
    return paths


class BundledFlutterEngine:
    def __init__(self, executable, system, mode):
        executable = executable.resolve(strict=True)
        self.bundle = bundle_root(executable, system)
        if system == "linux":
            self.entry = self.bundle / "lib/libflutter_linux_gtk.so"
        elif system == "macos":
            self.entry = self.bundle / "Contents/Frameworks/FlutterMacOS.framework/FlutterMacOS"
        else:
            raise ValueError("Bundled Flutter engine evidence requires Linux or macOS")
        self.system = system
        self.path = self.entry.resolve(strict=True)
        if not self.path.is_relative_to(self.bundle):
            raise RuntimeError("The bundled Flutter engine points outside the tested app bundle")
        self.identity = {"schemaVersion": 1, "platform": system, "buildMode": mode,
                         "binaryName": self.path.name, **binary_identity(self.path)}

    def verify_unchanged(self):
        if self.entry.resolve(strict=True) != self.path:
            raise RuntimeError("The bundled Flutter engine path changed during the smoke")
        actual = binary_identity(self.path)
        if any(actual[key] != self.identity[key] for key in actual):
            raise RuntimeError("The bundled Flutter engine bytes changed during the smoke")

    def observe_loaded(self, pid, *, raw_path=None):
        self.verify_unchanged()
        error, exit_code = "", 0
        if self.system == "linux":
            origin = f"/proc/{pid}/maps"
            output = Path(origin).read_text(encoding="utf-8")
        else:
            command = ["lsof", "-a", "-p", str(pid), "-d", "txt", "-Fpcfn0"]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
            origin, output = "lsof -a -p <owned PID> -d txt -Fpcfn0", result.stdout
            error, exit_code = result.stderr, result.returncode
        if raw_path is not None:
            raw_path.write_text(json.dumps({"source": origin, "pid": pid, "stdout": output,
                                           "stderr": error, "exitCode": exit_code}) + "\n", encoding="utf-8")
        if exit_code or error.strip():
            raise RuntimeError(f"Cannot observe the owned process's engine mappings: {error.strip()}")
        images = (linux_engine_images(output, self.path.name) if self.system == "linux"
                  else darwin_engine_images(output, pid, self.path.name))
        paths = {Path(image["path"]).resolve(strict=True) for image in images}
        if paths != {self.path}:
            raise RuntimeError("The owned process did not map exactly its bundled Flutter engine")
        if self.system == "linux":
            observed = self.path.stat()
            if any(image["inode"] != observed.st_ino
                   or image["device"] != (os.major(observed.st_dev), os.minor(observed.st_dev))
                   for image in images):
                raise RuntimeError("The mapped Flutter engine inode differs from the bundled file")
        self.verify_unchanged()
        return {"source": origin, "pid": pid, "canonicalPath": str(self.path),
                "bundleRelativePath": str(self.path.relative_to(self.bundle)),
                "identity": self.identity, "rawObservation": output}

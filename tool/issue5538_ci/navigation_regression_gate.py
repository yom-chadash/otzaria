#!/usr/bin/env python3
"""Validate the expected old navigation failure and the repaired viewer suite."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

BASELINE = "cdea38083d06b445b4ec67d8345109a0457924af"
CANDIDATE = "4f7d95e3d9e7d932a3675d837bfa3f8e7f3be703"
PRODUCT = "packages/pdfrx/lib/src/widgets/pdf_viewer.dart"
TEST = "packages/pdfrx/test/pdf_viewer_test.dart"
PRODUCT_SHA256 = "1b4bc0a7cf375fe3eac309af1cbc2a847dd238215313f14ba178a73315dbb2b9"
TEST_SHA256 = "1d1588d63d1148e25dc3d650d337ca5266ebf4335ada71f4e45bc59187e0cc90"
CORE_NAMES = {
    f"navigation retains page 280 through geometry changes (smart: {smart}, matrix: {matrix})"
    for smart in ("false", "true") for matrix in ("false", "true")
}
EXPECTED_REASON = "relayout must settle the original Future without an application timeout"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, encoding="utf-8").strip()


def save(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")


def source(root: Path) -> dict:
    files = {}
    for name in git(root, "ls-files").splitlines():
        path = root / name
        if path.is_symlink():
            files[name] = {"symlink": str(path.readlink())}
        else:
            files[name] = {"sha256": sha(path), "bytes": path.stat().st_size}
    return {"commit": git(root, "rev-parse", "HEAD"), "tree": git(root, "rev-parse", "HEAD^{tree}"), "files": files}


def check_sdk(path: Path) -> dict:
    sdk = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "frameworkVersion": "3.47.7",
        "frameworkRevision": "abaf9c523780a608bd46686fd5e53740a07077f8",
        "engineRevision": "deb287481e3ce9468f3434937ced4240a70539ca",
    }
    if any(sdk.get(k) != v for k, v in expected.items()) or sdk.get("dartSdkVersion", "").split()[0] != "3.13.5":
        raise ValueError("The navigation gate requires the locked Flutter/framework/engine/Dart identities")
    return sdk


def prepare(baseline: Path, candidate: Path, output: Path, sdk_file: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for root, commit in ((baseline, BASELINE), (candidate, CANDIDATE)):
        if git(root, "rev-parse", "HEAD") != commit or git(root, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Unexpected or modified source checkout")
    if set(git(candidate, "diff", "--name-only", BASELINE, CANDIDATE).splitlines()) != {PRODUCT, TEST}:
        raise ValueError("Candidate includes unrelated product changes")
    if sha(candidate / PRODUCT) != PRODUCT_SHA256 or sha(candidate / TEST) != TEST_SHA256:
        raise ValueError("Candidate bytes differ from the reviewed payload")
    before = source(baseline)
    (baseline / TEST).write_bytes((candidate / TEST).read_bytes())
    overlay = source(baseline)
    if {p for p in before["files"] if before["files"][p] != overlay["files"][p]} != {TEST}:
        raise ValueError("Only identical candidate tests may be overlaid on the baseline")
    patch = subprocess.check_output(["git", "-C", str(candidate), "diff", "--binary", BASELINE, CANDIDATE])
    (output / "reviewed-product-and-tests.patch").write_bytes(patch)
    save(output / "prepared-sources.json", {
        "schemaVersion": 1,
        "kind": "correctness-regression-gate-not-performance-measurement",
        "sdk": check_sdk(sdk_file),
        "baselineOriginal": before,
        "baselineWithIdenticalTestOverlay": overlay,
        "candidate": source(candidate),
        "identicalTestSha256": TEST_SHA256,
        "reviewedPatchSha256": hashlib.sha256(patch).hexdigest(),
    })


def verify_sources(baseline: Path, candidate: Path, output: Path, sdk_file: Path, stage: str) -> None:
    prepared = json.loads((output / "prepared-sources.json").read_text(encoding="utf-8"))
    if check_sdk(sdk_file) != prepared["sdk"]:
        raise ValueError("SDK identity drift")
    if source(baseline) != prepared["baselineWithIdenticalTestOverlay"] or source(candidate) != prepared["candidate"]:
        raise ValueError("Tracked source/input bytes changed during dependency resolution or tests")
    lock_hashes = [sha(p / "pubspec.lock") for p in (baseline, candidate)]
    if lock_hashes[0] != lock_hashes[1]:
        raise ValueError("Baseline and candidate must use the same resolved lock")
    resolved = {}
    for role, root in (("baseline", baseline), ("candidate", candidate)):
        resolved[role] = {name: sha(root / name) for name in ("pubspec.lock", ".dart_tool/package_config.json", ".dart_tool/package_graph.json")}
    if stage == "after":
        before = json.loads((output / "before-tests.json").read_text(encoding="utf-8"))
        if resolved != before["resolvedDependencies"]:
            raise ValueError("Resolved dependency/configuration drift during tests")
    save(output / f"{stage}-tests.json", {"schemaVersion": 1, "sourcesVerified": True, "sdkVerified": True, "resolvedDependencies": resolved})


def validate_report(path: Path, exit_code: int, baseline: bool) -> dict:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    starts = {r["test"]["id"]: r["test"] for r in records if r.get("type") == "testStart"}
    done = [r for r in records if r.get("type") == "done"]
    finished = [r for r in records if r.get("type") == "testDone"]
    errors = [r for r in records if r.get("type") == "error"]
    if len(done) != 1 or any(r["testID"] not in starts for r in finished):
        raise ValueError("Incomplete or malformed Flutter test stream")
    visible = [(starts[r["testID"]]["name"], r) for r in finished if not r.get("hidden", False)]
    if len({r["testID"] for r in finished}) != len(finished):
        raise ValueError("Duplicate test-ID completion")
    if any(r.get("skipped") for r in finished):
        raise ValueError("A skipped test cannot prove the regression gate")
    if baseline:
        if exit_code != 1 or done[0].get("success") is not False or len(visible) != 4 or {name for name, _ in visible} != CORE_NAMES:
            raise ValueError("Baseline did not execute exactly the four expected regression cases")
        if any(r.get("result") != "error" for _, r in visible):
            raise ValueError("Every original target280 case must fail on the unmodified baseline")
        expected_ids = {r["testID"] for _, r in visible}
        if len(errors) != 4 or {r.get("testID") for r in errors} != expected_ids:
            raise ValueError("Unexpected compilation/setup/teardown error in the negative control")
        for name, item in visible:
            test_id = item["testID"]
            error = next(r for r in errors if r["testID"] == test_id)
            wrapper = f"Test failed. See exception logs above.\nThe test description was: {name}"
            if error.get("isFailure") is not False or error.get("error", "").strip() != wrapper:
                raise ValueError("Baseline did not report the expected Flutter widget-test assertion wrapper")
            exceptions = [r.get("message", "") for r in records if r.get("type") == "print" and r.get("testID") == test_id and "EXCEPTION CAUGHT BY FLUTTER TEST FRAMEWORK" in r.get("message", "")]
            assertion = "The following TestFailure was thrown running a test:\nExpected: true\n  Actual: <false>\n" + EXPECTED_REASON
            if len(exceptions) != 1 or assertion not in exceptions[0] or "_registerNavigationLayoutTests" not in exceptions[0] or name not in exceptions[0]:
                raise ValueError("Baseline failed for a different reason than its unresolved navigation Future")
        if any(r.get("hidden", False) and r.get("result") != "success" for r in finished):
            raise ValueError("Baseline loading/setup did not succeed")
        classification = "expected-product-regression-reproduced"
    else:
        if exit_code != 0 or done[0].get("success") is not True or errors:
            raise ValueError("Candidate correctness suite did not complete successfully")
        if any(r.get("result") != "success" for r in finished):
            raise ValueError("Candidate includes a failed test")
        regressions = [name for name, _ in visible if name.startswith("navigation ")]
        if not CORE_NAMES.issubset(regressions) or len(regressions) != 11 or len(set(regressions)) != 11:
            raise ValueError("Candidate did not execute all eleven reviewed navigation cases")
        classification = "candidate-correctness-suite-passed"
    return {
        "schemaVersion": 1, "classification": classification,
        "reportSha256": sha(path), "exitCode": exit_code,
        "visibleTests": len(visible), "errors": len(errors),
        "cases": [{"name": name, "testId": item["testID"], "result": item["result"]} for name, item in visible],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "before", "after", "baseline", "candidate"))
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--exit-code", type=int)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.baseline.resolve(), args.candidate.resolve(), args.output, args.sdk)
    elif args.action in ("before", "after"):
        verify_sources(args.baseline.resolve(), args.candidate.resolve(), args.output, args.sdk, args.action)
    else:
        result = validate_report(args.report, args.exit_code, args.action == "baseline")
        save(args.output / f"{args.action}-validated.json", result)
        print(json.dumps({k: v for k, v in result.items() if k != "cases"}))


if __name__ == "__main__":
    main()

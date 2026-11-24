"""Fork-only Linux profile comparison of real N-up file and byte exports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tool/perf_probe"))
from posix_engine import BundledFlutterEngine
from run_perf_probe_posix import binary_path, capture, child_exit, cleanup_group, run_logged

PDFIUM_SHA256 = "1c251efce460d67437a4c76d4be9033ae2f17bf5603f5bff1af10209b94ca723"
FRAMEWORK_SHA = "abaf9c523780a608bd46686fd5e53740a07077f8"


def save(path, value):
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    pending.replace(path)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_receipt():
    if capture(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT).strip():
        raise ValueError("Measurement source must be clean.")
    return {"commit": capture(["git", "rev-parse", "HEAD"], cwd=ROOT).strip(),
            "tree": capture(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT).strip()}


def process_sample(pid):
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    status = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                  if ":" in line)
    return {"epochUs": time.time_ns() // 1000,
            "rssBytes": int(status.get("VmRSS", "0 kB").split()[0]) * 1024,
            "processLifetimePeakBytes": int(status.get("VmHWM", "0 kB").split()[0]) * 1024,
            "cpuSeconds": (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK"),
            "startTicks": int(stat[19])}


def launch(executable, engine, config, directory):
    directory.mkdir()
    config_path = directory / "config.json"
    save(config_path, {**config, "output": str(directory)})
    start = time.monotonic()
    samples = []
    observed = None
    ready = None
    with (directory / "app.log").open("w") as log:
        process = subprocess.Popen([str(executable)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True,
                                   env={**os.environ, "PDF_5538_EXPORT_CONFIG": str(config_path)})
        try:
            while child_exit(process) is None:
                if time.monotonic() - start > 900:
                    raise TimeoutError("Large export exceeded 15 minutes.")
                try:
                    sample = process_sample(process.pid)
                    phase_file = directory / "phase.json"
                    sample["phase"] = json.loads(phase_file.read_text())["phase"] if phase_file.exists() else "opening"
                    samples.append(sample)
                except FileNotFoundError:
                    continue
                if observed is None and (directory / "ready.json").exists():
                    ready = json.loads((directory / "ready.json").read_text())
                    if ready["pid"] != process.pid or ready["sourceCommit"] != config["sourceCommit"]:
                        raise ValueError("The export receipt came from a different process/source.")
                    observed = engine.observe_loaded(process.pid, raw_path=directory / "engine-mappings.json")
                    mappings = Path(f"/proc/{process.pid}/maps").read_text()
                    modules = {Path(line.split(maxsplit=5)[5]).resolve() for line in mappings.splitlines()
                               if len(line.split(maxsplit=5)) == 6 and line.split(maxsplit=5)[5].endswith("/libpdfium.so")}
                    if modules != {Path(config["pdfium"]).resolve()}:
                        raise ValueError("The process did not map exactly the verified PDFium module.")
                    observed["pdfium"] = {"sha256": config["pdfiumSha256"], "mappedPaths": [str(path) for path in modules]}
                    save(directory / "engine.json", observed)
                    (directory / "start").write_text("verified\n")
                time.sleep(.01)
            code = child_exit(process)
        finally:
            cleanup_group(process)
            save(directory / "host-samples.json", samples)
    engine.verify_unchanged()
    if code or observed is None:
        raise RuntimeError(f"Export {config['variant']} failed with exit {code}: {directory}")
    result = json.loads((directory / "result.json").read_text())
    if (result.get("status") != "passed" or result.get("variant") != config["variant"]
            or result.get("inputSha256") != config["inputSha256"] or result.get("mode") != "profile"):
        raise ValueError("Export receipt identity or status differs.")
    phases = [row for row in samples if row["phase"] == "compose"]
    if (not phases and config["variant"] in ("bytes", "file")) or len({row["startTicks"] for row in samples}) != 1:
        raise ValueError("Missing measured phase samples or reused PID.")
    result["host"] = {"samplingIntervalMs": 10, "composeSamples": len(phases),
                      "composeSampledRssPeakBytes": max((row["rssBytes"] for row in phases), default=None),
                      "composeSampledCpuSeconds": phases[-1]["cpuSeconds"] - phases[0]["cpuSeconds"] if phases else None,
                      "wholeProcessPeakBytes": max(row["processLifetimePeakBytes"] for row in samples)}
    save(directory / "validated.json", result)
    return result


def parity(rows):
    keys = ("inputSha256", "exportedPages", "outputPages", "allPagesTextSha256", "sampledPages")
    references = [{key: row[key] for key in keys} for row in rows if row["variant"] in ("bytes", "file")]
    if not references or any(row != references[0] for row in references):
        raise ValueError("File and bytes N-up text/raster/page parity failed.")
    return {"allOutputPageTextsEqual": True, "firstMiddleLastRastersEqual": True,
            "reference": references[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--pdfium", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=374)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("This experiment requires the pinned Linux x64 engine.")
    output = args.output.resolve()
    if output.is_relative_to(ROOT) or ROOT.is_relative_to(output):
        raise ValueError("Evidence must be outside the source checkout.")
    output.mkdir(parents=True, exist_ok=False)
    source = source_receipt()
    sdk = json.loads(capture(["flutter", "--version", "--machine"], cwd=ROOT, timeout=120))
    if sdk.get("frameworkRevision") != FRAMEWORK_SHA or sdk.get("frameworkVersion") != "3.47.7":
        raise ValueError("Flutter changed.")
    if digest(args.input) != args.input_sha256 or digest(args.pdfium) != PDFIUM_SHA256:
        raise ValueError("Corpus/native input digest changed.")
    report = {"schemaVersion": 1, "status": "running", "source": source, "sdk": sdk,
              "hardware": {"host": platform.node(), "machine": platform.machine()},
              "workflow": {key: os.environ.get(key) for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")},
              "rows": [], "limitations": ["Hosted Linux VM; not physical device or end-user printer performance.",
                "Profile standalone production export calls; no claim about full reader navigation.",
                "Peak RSS samples are lower bounds. Whole-process high-water mark includes validation.",
                "Six process launches per variant; exploratory medians, no p99 or broad noise gate."]}
    save(output / "report.json", report)
    try:
        run_logged(["flutter", "pub", "get", "--enforce-lockfile"], output / "resolve.log", cwd=ROOT, timeout=900)
        run_logged(["flutter", "build", "linux", "--profile", "--no-pub",
                    "--target=tool/issue5538_ci/export_profile_app.dart", "--dart-define=PDF_5538_EXPORT_PROBE=true"],
                   output / "build.log", cwd=ROOT, timeout=3600)
        if source_receipt() != source:
            raise ValueError("Source changed during build.")
        executable = binary_path(ROOT, "linux", "profile")
        engine = BundledFlutterEngine(executable, "linux", "profile")
        report["binarySha256"] = digest(executable)
        report["bundledEngine"] = engine.identity
        config = {"input": str(args.input.resolve()), "inputSha256": args.input_sha256,
                  "pdfium": str(args.pdfium.resolve()), "pdfiumSha256": PDFIUM_SHA256,
                  "pages": args.pages, "sourceCommit": source["commit"]}
        order = [("bytes", True), ("file", True)] + [(variant, False) for _ in range(3)
                                                     for variant in ("bytes", "file", "file", "bytes")]
        order += [("cancel", False), ("write-failure", False)]
        for index, (variant, warmup) in enumerate(order):
            row = launch(executable, engine, {**config, "variant": variant}, output / f"launch-{index:02}-{variant}")
            row.update(warmup=warmup, launchIndex=index)
            report["rows"].append(row)
            save(output / "report.json", report)
        report["parity"] = parity(report["rows"])
        report["summary"] = {}
        for variant in ("bytes", "file"):
            rows = [row for row in report["rows"] if row["variant"] == variant and not row["warmup"]]
            report["summary"][variant] = {"launches": len(rows),
                "medianComposeMs": statistics.median(row["composeUs"] / 1000 for row in rows),
                "medianExtraWriteMs": statistics.median(row["bytesResultWriteUs"] / 1000 for row in rows),
                "medianSampledPeakRssBytes": statistics.median(row["host"]["composeSampledRssPeakBytes"] for row in rows),
                "medianOutputBytes": statistics.median(row["outputBytes"] for row in rows)}
        if source_receipt() != source or digest(executable) != report["binarySha256"]:
            raise ValueError("Source or app binary changed during measurement.")
        report["status"] = "passed"
    except BaseException as error:
        report.update(status="failed", error=repr(error))
        raise
    finally:
        save(output / "report.json", report)


if __name__ == "__main__":
    main()

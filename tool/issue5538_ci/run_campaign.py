"""Measure balanced PDF source pairs while reusing two verified checkouts."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import shutil
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tool.perf_probe import run_pdf_pair as pair


def read_plan(path):
    plan = pair.read_json(path)
    fields = {"schemaVersion", "baseline", "candidate", "launchesPerBlock", "repetitions",
              "cases", "cacheStates", "scenarios"}
    if (not isinstance(plan, dict) or set(plan) != fields
            or type(plan["schemaVersion"]) is not int or plan["schemaVersion"] != 1):
        raise ValueError("Unknown campaign plan schema or fields.")
    for role in ("baseline", "candidate"):
        pair.full_sha(plan[role])
    for key, maximum in (("launchesPerBlock", 10), ("repetitions", 100)):
        if type(plan[key]) is not int or not 1 <= plan[key] <= maximum:
            raise ValueError(f"Invalid {key}.")
    for key, allowed in (("cacheStates", {"cold", "warm"}),
                         ("scenarios", {"pdf-reader", "pdf-book-view"})):
        values = plan[key]
        if (not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values)
                or len(values) != len(set(values)) or not set(values) <= allowed):
            raise ValueError(f"Invalid {key}.")
    cases = plan["cases"]
    if (not isinstance(cases, list) or not cases
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value)
                   for value in cases) or len(cases) != len(set(cases))):
        raise ValueError("Case IDs must be unique safe directory names.")
    return plan


def block_order(condition_index, block_index):
    roles = ("baseline", "candidate")
    return roles if (condition_index + block_index) % 2 == 0 else roles[::-1]


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def latency_summary(blocks, *, exclude_startup=False):
    collected = {}
    for block_index, reports in enumerate(blocks):
        for role, report in reports.items():
            for row in report["scenarios"]:
                if exclude_startup and row["name"].startswith("startup."):
                    continue
                if row.get("status") != "passed" or row.get("measurementKind") == "resource-observation":
                    continue
                clusters = collected.setdefault(row["name"], {}).setdefault(role, {})
                for sample in row["samples"]:
                    if sample.get("warmup") or sample.get("status") != "passed":
                        continue
                    duration = sample.get("durationMs")
                    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                        raise ValueError("Invalid measured action duration.")
                    clusters.setdefault((block_index, sample["launchIndex"]), []).append(duration)
    summary = {}
    for name, roles in collected.items():
        if set(roles) != {"baseline", "candidate"}:
            raise ValueError("Missing action latency from one source.")
        stats = {}
        for role, clusters in roles.items():
            values = list(itertools.chain.from_iterable(clusters.values()))
            medians = [statistics.median(values) for values in clusters.values()]
            center = statistics.median(medians)
            stats[role] = {
                "observations": len(values), "processLaunches": len(clusters),
                "p50Ms": statistics.median(values), "p95Ms": percentile(values, 0.95),
                "p99Ms": percentile(values, 0.99), "launchMediansMs": medians,
                "launchMedianAbsoluteDeviationMs": statistics.median(abs(value - center) for value in medians),
            }
        rng = random.Random(5538)
        estimates = []
        for _ in range(4000):
            medians = {}
            for role, clusters in roles.items():
                groups = list(clusters.values())
                sample = itertools.chain.from_iterable(rng.choices(groups, k=len(groups)))
                medians[role] = statistics.median(sample)
            estimates.append(medians["candidate"] - medians["baseline"])
        baseline, candidate = (stats[role]["p50Ms"] for role in ("baseline", "candidate"))
        summary[name] = {
            **stats, "medianDeltaMs": candidate - baseline,
            "medianDeltaPercent": (candidate - baseline) / baseline * 100 if baseline else None,
            "exploratoryClusterBootstrap95Ms": [percentile(estimates, 0.025), percentile(estimates, 0.975)],
        }
    return summary


def verify_launch_viewports(directory, report, launches, *, controlled_macos=False):
    viewport = report.get("metadata", {}).get("viewport")
    if not isinstance(viewport, dict) or any(
            type(viewport.get(key)) not in (int, float)
            or not math.isfinite(viewport[key]) or viewport[key] <= 0
            for key in ("width", "height", "devicePixelRatio")):
        raise ValueError("A measured viewport must have finite positive dimensions and DPR.")
    for index in range(launches):
        path = directory / f"launch-{index:03d}" / "phases.jsonl"
        records = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        observations = [row["viewport"] for row in records if row.get("type") == "meta" and "viewport" in row]
        if observations != [viewport]:
            raise ValueError(f"Viewport changed within the measured process launches: {path}.")
        if controlled_macos:
            notes = [row for row in records if row.get("name") == "pdf.controlled_viewport"]
            if (len(notes) != 1 or notes[0].get("startupComparisonExcluded") is not True
                    or viewport["width"] != 1000 or viewport["height"] != 600
                    or notes[0].get("width") != 1000 or notes[0].get("height") != 600):
                raise ValueError("macOS requires a controlled PDF client viewport in every launch.")
    return viewport


def campaign(args):
    source, output, corpus = (value.resolve() for value in (args.source, args.output, args.corpus))
    if (output == source or output.is_relative_to(source) or source.is_relative_to(output)
            or output == corpus or corpus.is_relative_to(output) or output.is_relative_to(corpus)):
        raise ValueError("Campaign output must be outside the source and corpus directories.")
    plan = read_plan(args.plan)
    commits = {role: plan[role] for role in ("baseline", "candidate")}
    identities = pair.verify_sources(source, commits)
    orchestrator_commit = pair.git(source, "rev-parse", "HEAD")
    pair.verify_checkout(source, orchestrator_commit)
    if (pair.source_identity(source, orchestrator_commit)["harnessObjects"] !=
            identities["sources"]["baseline"]["harnessObjects"]):
        raise ValueError("The orchestrator must use the same verified harness as the measured sources.")
    system = platform.system()
    if system not in ("Windows", "Linux", "Darwin"):
        raise ValueError("Native desktop measurements require Windows, Linux or macOS.")
    if system == "Windows" and args.mode == "release" and not re.fullmatch(r"[0-9a-f]{64}", args.expected_engine_sha256):
        raise ValueError("Windows release requires the verified engine digest.")
    if system != "Windows" and args.expected_engine_sha256:
        raise ValueError("A Windows engine digest cannot identify a POSIX engine.")
    if system != "Linux" and args.display_mode != "desktop":
        raise ValueError("Xvfb is Linux-only.")
    flutter, dart = (shutil.which(name) for name in ("flutter", "dart"))
    pwsh = shutil.which("pwsh") if system == "Windows" else None
    if not flutter or not dart or (system == "Windows" and not pwsh):
        raise ValueError("Required Flutter/Dart/PowerShell tools are missing.")
    output.mkdir(parents=True, exist_ok=False, mode=0o700 if os.name == "posix" else 0o777)
    for name in ("worktrees", "logs", "corpus", "conditions"):
        (output / name).mkdir()
    checkouts = {role: output / "worktrees" / role for role in commits}
    created, snapshots = [], {}
    receipt = {
        "schemaVersion": 1, "status": "running", **identities, "plan": plan,
        "mode": args.mode, "platform": system, "displayMode": args.display_mode,
        "hardwareId": f"campaign-{platform.node()}-{uuid.uuid4().hex}",
        "startedAtEpochMs": time.time_ns() // 1_000_000,
        "orchestratorCommit": orchestrator_commit,
        "orchestratorSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "summaryMethod": {"quantiles": "linear interpolation between sorted observations",
                          "bootstrap": "4000 resamples of whole process-launch clusters; random seed 5538"},
        "workflow": {key: os.environ.get(key) for key in (
            "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")},
        "conditions": [], "preparation": {},
        "excludedComparativeScenarios": ["startup.first_content", "startup.window_useful"] if system == "Darwin" else [],
        "limitations": [
            "Cold means fresh application data, not a flushed OS file cache.",
            "Single hosted VM per platform/mode; no physical GPU, printer or sleep/resume claim.",
            "Balanced ABBA/BAAB blocks, six process launches per variant at the default settings.",
            "Bootstrap intervals are exploratory with few launch clusters and no multiple-comparison adjustment.",
            "Tail percentiles are descriptive; no automatic performance pass/fail threshold is applied.",
            "The existing native runner rechecks incremental builds; no source or engine guard is bypassed.",
            "On macOS the PDF client viewport is controlled after startup; startup rows are diagnostic only and excluded from performance conclusions.",
        ],
    }
    def save():
        pair.write_json(output / "campaign.json", receipt)
    try:
        receipt["sdk"] = pair.sdk_identity(flutter, source)
        save()
        for role, checkout in checkouts.items():
            pair.git(source, "worktree", "add", "--detach", str(checkout), commits[role])
            created.append(checkout)
            pair.verify_checkout(checkout, commits[role])
            preparation = receipt["preparation"][role] = {}
            for stage, command in (("resolve", ["pub", "get", "--enforce-lockfile"]),
                                   ("analyze", ["analyze", "--no-pub"]),
                                   ("tests", ["test", "--no-pub", "--reporter", "expanded", *pair.PDF_TESTS])):
                preparation[stage] = pair.run_logged([flutter, *command], checkout, output / "logs" / f"{role}-{stage}.log")
                pair.verify_checkout(checkout, commits[role])
                save()
        for case_id in plan["cases"]:
            manifest = (corpus / f"{case_id}.json").resolve(strict=True)
            if not manifest.is_relative_to(corpus):
                raise ValueError("A case manifest escaped the verified corpus directory.")
            directory = output / "corpus" / case_id
            directory.mkdir()
            def canonicalize(input_case, canonical):
                pair.run_logged([dart, "tool/perf_probe/prepare_pdf_case.dart", str(input_case), str(canonical)],
                                checkouts["baseline"], output / "logs" / f"prepare-{case_id}.log")
            snapshots[case_id] = pair.PdfCaseSnapshot.prepare(manifest, directory, canonicalize)
        conditions = itertools.product(plan["cases"], plan["cacheStates"], plan["scenarios"])
        for condition_index, (case_id, cache_state, scenario) in enumerate(conditions):
            case = snapshots[case_id]
            if scenario == "pdf-book-view" and case.case["source"]["pageCount"] < 5:
                raise ValueError("Book-view measurements require at least five pages.")
            name = f"{case_id}-{cache_state}-{scenario}"
            archive = output / "conditions" / name
            archive.mkdir()
            condition = {"id": name, "status": "running", "pdfCase": case.receipt, "blocks": []}
            receipt["conditions"].append(condition)
            settings = {"mode": args.mode, "cacheState": cache_state, "scenario": scenario,
                        "launches": plan["launchesPerBlock"], "repetitions": plan["repetitions"],
                        "platform": system, "displayMode": args.display_mode}
            native_args = argparse.Namespace(mode=args.mode, cache_state=cache_state, scenario=scenario,
                launches=plan["launchesPerBlock"], repetitions=plan["repetitions"],
                expected_engine_sha256=args.expected_engine_sha256, display_mode=args.display_mode)
            measured_blocks = []
            for block_index in range(2):
                results = output / "results"
                results.mkdir()
                block = {"order": list(block_order(condition_index, block_index)), "status": "running", "stages": {}}
                condition["blocks"].append(block)
                save()
                for role in block["order"]:
                    checkout = checkouts[role]
                    pair.verify_checkout(checkout, commits[role])
                    if pair.sdk_identity(flutter, checkout) != receipt["sdk"]:
                        raise ValueError("SDK identity changed during the campaign.")
                    case.verify()
                    command = pair.native_arguments(checkout, native_args, results / role, case.path,
                                                    receipt["hardwareId"], system, pwsh)
                    block["stages"][role] = pair.run_logged(command, checkout, output / "logs" / f"{name}-{block_index}-{role}.log")
                    case.verify_run(results / role, pair.read_json(results / role / "report.json"))
                    pair.verify_checkout(checkout, commits[role])
                    save()
                pair.run_logged([dart, "tool/perf_probe/analyze.dart", str(results / "candidate"),
                                 "--baseline", str(results / "baseline")], checkouts["candidate"],
                                output / "logs" / f"{name}-{block_index}-comparison.log")
                reports = {role: pair.read_json(results / role / "report.json") for role in commits}
                for role, report in reports.items():
                    case.verify_run(results / role, report)
                    viewport = verify_launch_viewports(results / role, report, settings["launches"],
                                                       controlled_macos=system == "Darwin")
                    if "viewport" in receipt and receipt["viewport"] != viewport:
                        raise ValueError("The viewport changed across sources, blocks or corpus cases.")
                    receipt["viewport"] = viewport
                if system != "Windows":
                    pair.verify_posix_engines(results, reports, settings, system)
                block["comparison"] = pair.validate_reports(reports, commits, settings,
                    receipt["hardwareId"], receipt["sdk"], expected_corpus=case.comparison["source"])
                if system == "Darwin":
                    for row in block["comparison"]["scenarios"]:
                        if row["name"].startswith("startup."):
                            row["excludedFromPerformanceConclusions"] = True
                            row["reason"] = "The controlled client viewport was applied after startup."
                measured_blocks.append(reports)
                block["status"] = "passed"
                destination = archive / f"block-{block_index}"
                results.rename(destination)
                block["results"] = str(destination.relative_to(output))
                # Cold fixtures contain copied large PDFs; their verified receipts are retained.
                for role in commits:
                    data = destination / role / "data"
                    if data.is_symlink():
                        raise ValueError("Owned fixture data unexpectedly became a symlink.")
                    if data.exists():
                        shutil.rmtree(data)
                save()
            condition["latency"] = latency_summary(measured_blocks, exclude_startup=system == "Darwin")
            condition["status"] = "passed"
            pair.write_json(archive / "condition.json", condition)
            save()
        for role, checkout in checkouts.items():
            pair.verify_checkout(checkout, commits[role])
            if pair.sdk_identity(flutter, checkout) != receipt["sdk"]:
                raise ValueError("SDK identity changed after the campaign.")
        for case in snapshots.values():
            case.verify()
        receipt["status"] = "passed"
    except BaseException as error:
        receipt.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        cleanup_errors = []
        for checkout in reversed(created):
            try:
                pair.git(source, "worktree", "remove", "--force", str(checkout))
            except Exception as error:
                cleanup_errors.append(str(error))
        receipt["endedAtEpochMs"] = time.time_ns() // 1_000_000
        if cleanup_errors:
            receipt.update(status="failed", cleanupErrors=cleanup_errors)
        save()
        if cleanup_errors:
            raise RuntimeError("Owned campaign worktrees could not be removed.")
    print(f"Verified {len(receipt['conditions'])} balanced conditions: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("profile", "release"), default="profile")
    parser.add_argument("--display-mode", choices=("desktop", "xvfb"), default="desktop")
    parser.add_argument("--expected-engine-sha256", default="")
    campaign(parser.parse_args())


if __name__ == "__main__":
    main()

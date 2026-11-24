"""Validate complete profile runs and compare matched app-highlight workloads."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

import validate_pdf_proximity_profile as proximity


QUERIES = ("literal_alef", "hebrew_combining_marks", "result_lines_alef_bet")
SCENARIOS = ("cold", "cache_hit", "params_change", "dimensions_change")
PDFIUM_SHA256 = "1c251efce460d67437a4c76d4be9033ae2f17bf5603f5bff1af10209b94ca723"


def query_ids(report):
    suite = report.get("querySuite", "legacy-v1")
    require(suite in ("legacy-v1", proximity.SUITE), "Unknown highlight query suite")
    return proximity.QUERIES if suite == proximity.SUITE else QUERIES


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def integer(value):
    return type(value) is int and value >= 0


def fingerprint(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def distribution(values):
    require(bool(values), "Empty sample distribution")
    values = sorted(values)
    return {
        "count": len(values), "min": values[0], "mean": sum(values) / len(values),
        "p50": values[math.ceil(.50 * len(values)) - 1], "median": statistics.median(values),
        "p95": values[math.ceil(.95 * len(values)) - 1], "max": values[-1],
    }


def validate_report(path):
    report = json.loads(path.read_text(encoding="utf-8"))
    require(report.get("protocolVersion") == 1 and report.get("status") == "passed", "Incomplete profile report")
    require(report.get("executionMode") == "flutter-profile" and report.get("component") == "otzaria-page-highlights-and-book-layout", "Unexpected measured component/mode")
    require(report.get("pdfiumSha256") == PDFIUM_SHA256 and report.get("dartVersion", "").startswith("3.13.5 "), "Different native runtime")
    require(re.fullmatch(r"[0-9a-f]{40}", report.get("sourceCommit", "")) is not None and fingerprint(report.get("highlightSourceSha256")), "Missing compiled source identity")
    require(fingerprint(report.get("inputSha256")), "Missing corpus identity")
    require(report.get("uiLatencyCollected") is False and report.get("microtaskAuditTimed") is False, "Unsupported measurement scope")
    require(report.get("contextLength") == 80 and report.get("maxCachedPages") == 8, "Highlight configuration changed")
    require(report.get("normalizeHebrewText") is True and report.get("decodeLegacyHebrewText") is True, "Normalization policy changed")
    require(report.get("highlightScope") == "actual app ensurePage with preloaded real structured text; extraction excluded", "Highlight timing scope changed")
    count, warmup, repetitions = (report[name] for name in ("sourcePageCount", "warmup", "repetitions"))
    require(integer(count) and count > 0 and integer(warmup) and integer(repetitions) and repetitions > 0, "Invalid workload size")
    queries = query_ids(report)
    require(tuple(query["id"] for query in report["queries"]) == queries, "Query set changed")
    inventory = report["textInventory"]
    require(len(inventory) == count, "Missing native text inventory")
    for number, page in enumerate(inventory, 1):
        require(page["pageNumber"] == number and fingerprint(page["textSha256"]), "Invalid native text identity")
        require(all(integer(page[name]) for name in ("textCharacters", "characterRects", "fragments")), "Invalid text inventory")
    expected = {(iteration, query, page) for iteration in range(warmup + repetitions) for query in queries for page in range(1, count + 1)}
    rows = report["observations"]
    require(len(rows) == len(expected), "Missing measured observations")
    seen, signatures = set(), {}
    for row in rows:
        key = (row["iteration"], row["queryId"], row["pageNumber"])
        require(key in expected and key not in seen and row["parity"] is True, "Missing, duplicate, or failed parity row")
        seen.add(key)
        require(row["warmup"] == (row["iteration"] < warmup), "Warmup entered measured samples")
        require(all(integer(row[name]) for name in ("elapsedUs", "matches", "rects")) and fingerprint(row["resultsSha256"]), "Invalid result/timing")
        signature = (row["matches"], row["rects"], row["resultsSha256"])
        require(signatures.setdefault(key[1:], signature) == signature, "Result geometry/context changed across iterations")
    streamed = [json.loads(line) for line in (path.parent / "observations.jsonl").read_text(encoding="utf-8").splitlines()]
    require(streamed == rows, "JSONL observations differ from the report")
    audit = report["microtaskAudit"]
    require(len(audit) == count * len(queries), "Incomplete untimed microtask audit")
    seen = set()
    for row in audit:
        key = (row["queryId"], row["pageNumber"])
        require(key in signatures and key not in seen and row["parity"] is True, "Missing, duplicate, or failed audit row")
        seen.add(key)
        require(integer(row["scheduledMicrotasks"]), "Invalid microtask count")
        require((row["matches"], row["resultsSha256"]) == (signatures[key][0], signatures[key][2]), "Audit workload differs from timed results")
    reference = report["referenceMatchesPerQuery"]
    require(set(reference) == set(queries), "Missing reference totals")
    for query in queries:
        total = sum(value[0] for key, value in signatures.items() if key[0] == query)
        require(total > 0 and reference[query] == total, "Empty workload or incorrect reference totals")
    if report.get("querySuite") == proximity.SUITE:
        proximity.validate(report, signatures)
    inputs = {("real-document-metadata", count)} | {
        (kind, length) for kind in ("derived-real-metadata-repeated", "synthetic-mixed-sizes") for length in (100, 1000, 10000)
    }
    expected_layout = {(kind, length, iteration, scenario) for kind, length in inputs for iteration in range(warmup + repetitions) for scenario in SCENARIOS}
    require(len(report["layout"]) == len(expected_layout), "Incomplete layout scenarios")
    seen, layout_signatures = set(), {}
    for row in report["layout"]:
        key = (row["inputKind"], row["pageCount"], row["iteration"], row["scenario"])
        require(key in expected_layout and key not in seen and row["parity"] is True, "Missing, duplicate, or failed layout row")
        seen.add(key)
        require(row["warmup"] == (row["iteration"] < warmup), "Layout warmup entered measured samples")
        require(row["sourceDocumentPageCount"] == (None if row["inputKind"] == "synthetic-mixed-sizes" else count), "Synthetic/repeated metadata mislabeled as a real document")
        require(row["sameLayoutInstance"] == (row["scenario"] == "cache_hit"), "Layout cache contract failed")
        require(all(integer(row[name]) for name in ("elapsedUs", "widthReads", "heightReads")) and fingerprint(row["layoutSha256"]), "Invalid layout metrics")
        invariant = (row["widthReads"], row["heightReads"], row["layoutSha256"])
        require(layout_signatures.setdefault((key[0], key[1], key[3]), invariant) == invariant, "Layout results/counters changed between iterations")
    require(not (path.parent / "failure.json").exists(), "Profile run also recorded failure")
    return report


def summarize(reports):
    result = {"highlight": {}, "layout": {}}
    for query in query_ids(reports[0]):
        samples = []
        tasks = []
        process_medians = []
        for report in reports:
            passes = []
            for iteration in range(report["warmup"], report["warmup"] + report["repetitions"]):
                passes.append(sum(row["elapsedUs"] for row in report["observations"] if row["queryId"] == query and row["iteration"] == iteration))
            samples.extend(passes)
            process_medians.append(statistics.median(passes))
            tasks.append(sum(row["scheduledMicrotasks"] for row in report["microtaskAudit"] if row["queryId"] == query))
        result["highlight"][query] = {
            "completePassUs": distribution(samples), "untimedScheduledMicrotasks": distribution(tasks),
            "processMedianUs": distribution(process_medians), "processMediansUs": process_medians,
            "completePassScope": "descriptive repeated passes; not independent sample n",
            "matchesPerPass": reports[0]["referenceMatchesPerQuery"][query],
        }
    keys = {(row["inputKind"], row["pageCount"], row["scenario"]) for row in reports[0]["layout"]}
    for key in sorted(keys):
        rows = [row for report in reports for row in report["layout"] if (row["inputKind"], row["pageCount"], row["scenario"]) == key and not row["warmup"]]
        result["layout"]["/".join(map(str, key))] = {name: distribution([row[name] for row in rows]) for name in ("elapsedUs", "widthReads", "heightReads")}
    return result


def compare(baselines, candidates):
    require(baselines and len(baselines) == len(candidates), "Require the same number of baseline/candidate launches")
    first = baselines[0]
    comparison_keys = (
        "inputSha256", "inputBytes", "pdfiumSha256", "sourcePageCount", "warmup", "repetitions", "contextLength", "maxCachedPages",
        "normalizeHebrewText", "decodeLegacyHebrewText", "queries", "textInventory", "referenceMatchesPerQuery", "host", "dartVersion",
    )
    def match_signatures(report):
        return {(row["queryId"], row["pageNumber"]): (row["matches"], row["rects"], row["resultsSha256"]) for row in report["observations"]}
    def layout_signatures(report):
        return {(row["inputKind"], row["pageCount"], row["scenario"]): row["layoutSha256"] for row in report["layout"]}
    for report in baselines + candidates:
        require(query_ids(report) == query_ids(first), "Query suite changed between paired processes")
        require(all(report[key] == first[key] for key in comparison_keys), "Hardware/runtime/corpus/query workload differs between compared runs")
        require(match_signatures(report) == match_signatures(first) and layout_signatures(report) == layout_signatures(first), "Baseline/candidate result or geometry parity failed")
    for group in (baselines, candidates):
        require(len({(report["sourceCommit"], report["highlightSourceSha256"]) for report in group}) == 1, "Compiled source changes within one side of the pair")
    if first.get("querySuite") == proximity.SUITE:
        proximity.compare_metadata(baselines + candidates)
    before, after = summarize(baselines), summarize(candidates)
    changes = {}
    for query in query_ids(first):
        old, new = before["highlight"][query], after["highlight"][query]
        old_us, new_us = old["processMedianUs"]["median"], new["processMedianUs"]["median"]
        changes[query] = {
            "p50BeforeUs": old_us, "p50AfterUs": new_us,
            "p50ChangePercent": None if old_us == 0 else (new_us - old_us) * 100 / old_us,
            "microtasksBefore": old["untimedScheduledMicrotasks"]["p50"],
            "microtasksAfter": new["untimedScheduledMicrotasks"]["p50"],
            "matchesPerPass": old["matchesPerPass"],
        }
    return {
        "protocolVersion": 1, "status": "passed", "meaning": "complete equivalent measurements; no speedup threshold assumed",
        "baseline": before, "candidate": after, "highlightChanges": changes,
        "source": {"baseline": first["sourceCommit"], "candidate": candidates[0]["sourceCommit"]},
        "launchesPerSide": len(baselines), "inputSha256": first["inputSha256"],
        "host": first["host"], "uiLatencyCollected": False,
        "querySuite": first.get("querySuite", "legacy-v1"),
        "primaryStatistic": "median of per-process medians of complete non-warmup passes",
        "statisticalUnit": "fresh app process; repeated passes are dependent",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, action="append", required=True)
    parser.add_argument("--candidate", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), "Comparison output already exists")
    result = compare([validate_report(path) for path in args.baseline], [validate_report(path) for path in args.candidate])
    result["reports"] = {str(path): digest(path) for path in args.baseline + args.candidate}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "output": str(args.output), "highlightChanges": result["highlightChanges"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Prove real product Proximity routing and preserve process-level ABBA samples."""

import hashlib
import html
import json
import statistics


SUITE = "product-proximity-v1"
RECIPE = "first-two-fixed-real-word-pairs-with-positive-ordered-distance-2-span-v1"
CANDIDATE_PAIRS = [
    ["אבן", "עזרא"], ["ויאמר", "אל"], ["יום", "זה"], ["את", "יום"],
    ["כי", "יום"], ["כי", "כל"], ["את", "אשר"], ["יום", "אחד"],
    ["אשר", "את"], ["על", "כל"], ["אל", "יום"], ["כי", "לא"],
]
CONFIGURATIONS = [
    (pair, distance, any_order)
    for pair in (1, 2) for distance in (2, 10) for any_order in (False, True)
]
QUERIES = tuple(
    f"proximity_pair_{pair}_d{distance}_{'any_order_reversed' if any_order else 'ordered'}"
    for pair, distance, any_order in CONFIGURATIONS
)
ABBA = ("baseline", "candidate", "candidate", "baseline")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value):
    return type(value) is int and value >= 0


def sha_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha_json(value):
    return sha_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def utf16_length(value):
    return len(value.encode("utf-16-le")) // 2


def utf16_slice(value, start, end):
    return value.encode("utf-16-le")[2 * start:2 * end].decode("utf-16-le")


def valid_span(span, length):
    return (isinstance(span, list) and len(span) == 2 and
            all(integer(value) for value in span) and 0 <= span[0] < span[1] <= length)


def validate(report, signatures):
    count = report["sourcePageCount"]
    require(report.get("querySuite") == SUITE, "Missing product Proximity suite")
    require(report.get("measurementTarget") == "actual product advanced-search highlight helper; derived native-text snippets; search retrieval and UI excluded", "Proximity timing scope changed")
    require(integer(report.get("processId")) and report["processId"] > 0, "Missing app process identity")
    require(tuple(query["id"] for query in report["queries"]) == QUERIES, "Incomplete Proximity configuration grid")
    require(report.get("queryDescriptorsSha256") == sha_json(report["queries"]), "Query descriptor hash differs")
    engine = report["searchEngine"]
    require(engine.get("initializer") == "SearchEngineLibrary.ensureLoaded" and engine.get("packageVersion") == "7.2.1", "Different native search initialization")
    require(engine.get("mappedIntoProcess") is True and engine.get("initializationTimed") is False, "Search engine was not mapped before timing")
    require(integer(engine.get("libraryBytes")) and engine["libraryBytes"] > 0, "Invalid search engine binary receipt")
    require(isinstance(engine.get("librarySha256"), str) and len(engine["librarySha256"]) == 64 and all(char in "0123456789abcdef" for char in engine["librarySha256"]), "Invalid search engine hash")
    require(isinstance(engine.get("libraryPath"), str) and engine["libraryPath"].endswith("/lib/libsearch_engine.so"), "Unexpected mapped search library")
    selection = report["querySelection"]
    require(selection.get("recipe") == RECIPE and selection.get("candidatePairs") == CANDIDATE_PAIRS, "Query selection recipe changed")
    for name in ("selectionTimed", "builderTimed", "searchIndexExecuted", "nativeTextModified", "selectionUsesPerformance"):
        require(selection.get(name) is False, f"Unsupported Proximity scope: {name}")
    require(selection.get("spanComputation") == "Proximity allMatches eagerly computes spans in both sources", "Missing eager-span scope limitation")
    attempts = selection["attempts"]
    require(2 <= len(attempts) <= len(CANDIDATE_PAIRS), "Missing selection attempts")
    positives = []
    for index, attempt in enumerate(attempts):
        require(attempt["candidateIndex"] == index and attempt["query"] == " ".join(CANDIDATE_PAIRS[index]), "Candidate order or real-word query changed")
        require(type(attempt["positive"]) is bool and integer(attempt["pagesExamined"]) and 1 <= attempt["pagesExamined"] <= count, "Invalid selection evidence")
        if attempt["positive"]:
            require(attempt["witnessPageNumber"] == attempt["pagesExamined"], "Selection did not retain the first positive page")
            positives.append(index)
        else:
            require(attempt["witnessPageNumber"] is None and attempt["pagesExamined"] == count, "Negative candidate did not examine the corpus")
    require(len(positives) == 2 and positives[-1] == len(attempts) - 1, "Selection did not stop at its second positive pair")
    witnesses = selection["selectedPairs"]
    require(len(witnesses) == 2, "Missing selected pair witnesses")
    for pair_index, witness in enumerate(witnesses, 1):
        require(witness["pairIndex"] == pair_index and witness["candidateIndex"] == positives[pair_index - 1], "Selected pair differs from attempted order")
        require(witness["terms"] == CANDIDATE_PAIRS[witness["candidateIndex"]], "Selected terms are not the pinned real-word pair")
        page = witness["pageNumber"]
        require(integer(page) and 1 <= page <= count, "Witness page is outside corpus")
        inventory = report["textInventory"][page - 1]
        require(witness["pageTextSha256"] == inventory["textSha256"], "Witness native text differs from the measured text")
        require(valid_span(witness["span"], inventory["textCharacters"]), "Invalid witness span")
        start, end = witness["snippetStart"], witness["snippetEnd"]
        require(integer(start) and integer(end) and 0 <= start <= witness["span"][0] < witness["span"][1] <= end <= inventory["textCharacters"], "Invalid witness snippet bounds")
        require(utf16_length(witness["snippetText"]) == end - start, "Witness snippet length does not match native offsets")
        require(utf16_slice(witness["snippetText"], witness["span"][0] - start, witness["span"][1] - start) == witness["spanText"], "Selected span text changed")
        require(witness["snippetTextSha256"] == sha_text(witness["snippetText"]) and witness["snippetHtmlSha256"] == sha_text(witness["snippetHtml"]), "Witness snippet hash differs")
        require(witness["snippetHtml"] == html.escape(witness["snippetText"], quote=False), "Snippet HTML altered native text")
        require(witness["snippetOrigin"] == "derived-native-page-text; not an index search result", "Derived snippet mislabeled as index retrieval")
    reference = report["referenceSpans"]
    require(len(reference) == count * len(QUERIES), "Incomplete raw span evidence")
    seen, spans_by_page = set(), {}
    for row in reference:
        key = row["queryId"], row["pageNumber"]
        require(key in signatures and key not in seen, "Missing/duplicate raw span row")
        seen.add(key)
        require(len(row["spans"]) == signatures[key][0] and row["resultsSha256"] == signatures[key][2], "Raw span count/geometry differs from timed results")
        require(row["spansSha256"] == sha_json(row["spans"]), "Raw span hash differs")
        require(all(valid_span(span, report["textInventory"][key[1] - 1]["textCharacters"]) for span in row["spans"]), "Raw match span is outside native text")
        spans_by_page[key] = row["spans"]
    for descriptor, (pair_index, distance, any_order) in zip(report["queries"], CONFIGURATIONS, strict=True):
        witness = witnesses[pair_index - 1]
        terms = list(reversed(witness["terms"])) if any_order else witness["terms"]
        require(descriptor["kind"] == "ProximityHighlightPattern" and descriptor["observedPatternType"] == "ProximityHighlightPattern", "Actual builder selected another pattern type")
        require(descriptor["builder"] == "PdfBookSearchView.buildAdvancedHighlightPattern" and descriptor["productRoute"] == "advanced-search-page-highlights", "Workload bypasses the actual product builder")
        require(descriptor["pairIndex"] == pair_index and descriptor["candidateIndex"] == witness["candidateIndex"], "Descriptor changed its selected pair")
        require(descriptor["terms"] == terms and descriptor["query"] == " ".join(terms), "Actual pattern query differs from its descriptor")
        require(descriptor["queryOrdering"] == ("reversed" if any_order else "original") and descriptor["isSimpleSearch"] is False, "Query did not use the advanced route/order")
        settings = {"searchMode": "advanced", "distance": distance, "proximityScope": "wordDistance", "wordMatchMode": "all", "wordMatchCount": 2, "querySemantics": "regular", "anyOrder": any_order, "searchOptions": {}, "alternativeWords": {}, "spacingValues": {}}
        require(descriptor["settings"] == settings, "Proximity policy differs from the declared product workload")
        require(json.loads(descriptor["patternKey"]) == [descriptor["query"], "advanced", distance, {}, {}, {}, "regular", "wordDistance", "all", 2, any_order], "Actual pattern key differs from effective settings")
        require(descriptor["witness"] == witness, "Descriptor witness differs from selection evidence")
        overlaps = descriptor["positiveWitnessSpans"]
        require(bool(overlaps) and all(span in spans_by_page[(descriptor["id"], witness["pageNumber"])] and span[0] < witness["span"][1] and span[1] > witness["span"][0] for span in overlaps), "Actual pattern has no retained positive overlap with its real-text witness")


def compare_metadata(reports):
    first = reports[0]
    for report in reports:
        require(all(report[key] == first[key] for key in ("querySuite", "querySelection", "queryDescriptorsSha256", "referenceSpans", "measurementTarget")), "Proximity selection/spans differ across paired processes")
        require(all(report["searchEngine"][key] == first["searchEngine"][key] for key in ("initializer", "packageVersion", "librarySha256", "libraryBytes", "mappedIntoProcess", "initializationTimed")), "Native search engine differs across compared processes")


def complete_passes(report, query):
    return [sum(row["elapsedUs"] for row in report["observations"] if row["queryId"] == query and row["iteration"] == iteration)
            for iteration in range(report["warmup"], report["warmup"] + report["repetitions"])]


def change_percent(before, after):
    return None if before == 0 else (after - before) * 100 / before


def summarize_abba(launches):
    """Keep each process, adjacent AB/BA pair, and block; never pool pass n."""
    require(len(launches) > 0 and len(launches) % 4 == 0, "Incomplete ABBA blocks")
    require(tuple(launch["side"] for launch in launches) == ABBA * (len(launches) // 4), "Launches do not retain actual ABBA order")
    query_ids = tuple(query["id"] for query in launches[0]["report"]["queries"])
    processes = []
    for index, launch in enumerate(launches):
        report = launch["report"]
        require(tuple(query["id"] for query in report["queries"]) == query_ids, "ABBA process query set differs")
        processes.append({
            "launchIndex": index, "block": index // 4 + 1, "positionInBlock": index % 4 + 1,
            "side": launch["side"], "sourceCommit": report["sourceCommit"],
            "processId": report.get("processId"), "reportPath": launch["reportPath"],
            "reportSha256": launch["reportSha256"],
            "queries": {query: {"completePassUs": complete_passes(report, query),
                                  "medianPassUs": statistics.median(complete_passes(report, query)),
                                  "matchesPerPass": report["referenceMatchesPerQuery"][query]}
                        for query in query_ids},
        })
    blocks, pairs = [], []
    for block in range(len(processes) // 4):
        group = processes[block * 4:block * 4 + 4]
        block_queries = {}
        for query in query_ids:
            before = [item["queries"][query]["medianPassUs"] for item in group if item["side"] == "baseline"]
            after = [item["queries"][query]["medianPassUs"] for item in group if item["side"] == "candidate"]
            block_queries[query] = {"baselineProcessMediansUs": before, "candidateProcessMediansUs": after,
                                    "changePercent": change_percent(statistics.median(before), statistics.median(after))}
        blocks.append({"block": block + 1,
                       "statistic": "percent change between medians of the two process medians on each side; not a geometric mean of pair ratios",
                       "queries": block_queries})
        for a, b in ((group[0], group[1]), (group[3], group[2])):
            pairs.append({"block": block + 1, "order": "AB" if a["launchIndex"] < b["launchIndex"] else "BA",
                          "statistic": "percent change of candidate process median relative to its adjacent baseline process median",
                          "baselineLaunchIndex": a["launchIndex"], "candidateLaunchIndex": b["launchIndex"],
                          "queries": {query: {"baselineMedianUs": a["queries"][query]["medianPassUs"],
                                               "candidateMedianUs": b["queries"][query]["medianPassUs"],
                                               "changePercent": change_percent(a["queries"][query]["medianPassUs"], b["queries"][query]["medianPassUs"])}
                                      for query in query_ids}})
    return {"statisticalUnit": "fresh app process; repeated passes within a process are dependent",
            "blocks": blocks, "adjacentPairs": pairs, "processes": processes,
            "processesPerSide": len(launches) // 2, "pairedComparisons": len(pairs),
            "limitations": ["Single host; process and OS/native caches are not independent experiments.",
                            f"{len(launches) // 4} ABBA blocks provide {len(launches) // 2} process medians per side; tail estimates are descriptive.",
                            "Extraction, query selection, builder construction, and microtask auditing are outside timing.",
                            "Derived real-text snippets prove product routing but do not measure index retrieval or UI latency.",
                            "Proximity eagerly computes spans in both variants; no nonblocking claim follows from fewer microtasks."]}

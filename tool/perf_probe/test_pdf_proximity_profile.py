import copy
import json
from pathlib import Path
import tempfile
import unittest

import test_pdf_highlight_profile as legacy
import validate_pdf_highlight_profile as profile
import validate_pdf_proximity_profile as proximity


def fixture(*, commit="a" * 40):
    value = legacy.fixture(count=2, commit=commit)
    native_text = "אבן עזרא ויאמר אל"
    value.update(querySuite=proximity.SUITE, processId=123,
                 measurementTarget="actual product advanced-search highlight helper; derived native-text snippets; search retrieval and UI excluded")
    value["searchEngine"] = {
        "initializer": "SearchEngineLibrary.ensureLoaded", "packageVersion": "7.2.1",
        "libraryPath": "/fixture/bundle/lib/libsearch_engine.so", "librarySha256": "6" * 64,
        "libraryBytes": 100, "mappedIntoProcess": True, "initializationTimed": False,
    }
    for page in value["textInventory"]:
        page.update(textSha256=proximity.sha_text(native_text), textCharacters=len(native_text))
    witnesses = []
    for index, span in enumerate(([0, 8], [9, 17])):
        witnesses.append({
            "pairIndex": index + 1, "candidateIndex": index, "terms": proximity.CANDIDATE_PAIRS[index],
            "pageNumber": 1, "pageTextSha256": proximity.sha_text(native_text), "span": span,
            "spanText": native_text[span[0]:span[1]], "snippetStart": 0, "snippetEnd": len(native_text),
            "snippetText": native_text, "snippetTextSha256": proximity.sha_text(native_text),
            "snippetHtml": native_text, "snippetHtmlSha256": proximity.sha_text(native_text),
            "snippetOrigin": "derived-native-page-text; not an index search result",
        })
    value["querySelection"] = {
        "recipe": proximity.RECIPE, "candidatePairs": proximity.CANDIDATE_PAIRS,
        "attempts": [{"candidateIndex": index, "query": " ".join(proximity.CANDIDATE_PAIRS[index]),
                      "pagesExamined": 1, "positive": True, "witnessPageNumber": 1} for index in range(2)],
        "selectedPairs": witnesses, "selectionTimed": False, "builderTimed": False,
        "searchIndexExecuted": False, "nativeTextModified": False, "selectionUsesPerformance": False,
        "spanComputation": "Proximity allMatches eagerly computes spans in both sources",
    }
    queries = []
    for query_id, (pair_index, distance, any_order) in zip(proximity.QUERIES, proximity.CONFIGURATIONS, strict=True):
        witness = witnesses[pair_index - 1]
        terms = list(reversed(witness["terms"])) if any_order else witness["terms"]
        query = " ".join(terms)
        queries.append({
            "id": query_id, "kind": "ProximityHighlightPattern",
            "builder": "PdfBookSearchView.buildAdvancedHighlightPattern",
            "observedPatternType": "ProximityHighlightPattern", "productRoute": "advanced-search-page-highlights",
            "pairIndex": pair_index, "candidateIndex": pair_index - 1, "terms": terms, "query": query,
            "queryOrdering": "reversed" if any_order else "original",
            "patternKey": json.dumps([query, "advanced", distance, {}, {}, {}, "regular", "wordDistance", "all", 2, any_order]),
            "isSimpleSearch": False,
            "settings": {"searchMode": "advanced", "distance": distance, "proximityScope": "wordDistance",
                         "wordMatchMode": "all", "wordMatchCount": 2, "querySemantics": "regular", "anyOrder": any_order,
                         "searchOptions": {}, "alternativeWords": {}, "spacingValues": {}},
            "witness": witness, "positiveWitnessSpans": [witness["span"]],
        })
    value["queries"] = queries
    value["queryDescriptorsSha256"] = proximity.sha_json(queries)
    value["referenceMatchesPerQuery"] = {query: 2 for query in proximity.QUERIES}
    value["observations"] = [
        {"iteration": iteration, "warmup": iteration == 0, "queryId": query, "pageNumber": page,
         "elapsedUs": 100, "matches": 1, "rects": 1, "resultsSha256": "1" * 64, "parity": True}
        for iteration in range(2) for query in proximity.QUERIES for page in (1, 2)
    ]
    value["microtaskAudit"] = [
        {"queryId": query, "pageNumber": page, "scheduledMicrotasks": 3, "matches": 1,
         "resultsSha256": "1" * 64, "parity": True}
        for query in proximity.QUERIES for page in (1, 2)
    ]
    value["referenceSpans"] = [
        {"queryId": query["id"], "pageNumber": page, "spans": [query["witness"]["span"]],
         "spansSha256": proximity.sha_json([query["witness"]["span"]]), "resultsSha256": "1" * 64}
        for query in queries for page in (1, 2)
    ]
    return value


class ProximityValidationTest(unittest.TestCase):
    def validate(self, value):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            path.with_name("observations.jsonl").write_text("".join(json.dumps(row) + "\n" for row in value["observations"]), encoding="utf-8")
            return profile.validate_report(path)

    def test_actual_builder_policy_and_real_text_witness_are_required(self):
        self.validate(fixture())
        mutations = (
            lambda value: value["queries"][0].update(observedPatternType="RegExp"),
            lambda value: value["queries"][0].update(isSimpleSearch=True),
            lambda value: value["queries"][0]["settings"].update(distance=0),
            lambda value: value["queries"][0].update(positiveWitnessSpans=[[0, 1]]),
            lambda value: value["querySelection"].update(selectionUsesPerformance=True),
            lambda value: value["querySelection"]["selectedPairs"][0].update(pageTextSha256="f" * 64),
            lambda value: value["referenceSpans"][0].update(spansSha256="f" * 64),
            lambda value: value["querySelection"]["attempts"][0].update(pagesExamined=2),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = fixture()
                mutate(value)
                value["queryDescriptorsSha256"] = proximity.sha_json(value["queries"])
                with self.assertRaises(ValueError):
                    self.validate(value)

    def test_native_search_binary_must_be_mapped_and_equal_across_sources(self):
        value = fixture()
        value["searchEngine"]["mappedIntoProcess"] = False
        with self.assertRaises(ValueError):
            self.validate(value)
        before, after = fixture(), fixture(commit="b" * 40)
        after["searchEngine"]["librarySha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "Native search engine"):
            profile.compare([before], [after])

    def test_span_or_selection_drift_is_not_hidden_by_same_timing(self):
        before, after = fixture(), fixture(commit="b" * 40)
        after["referenceSpans"][0]["spans"] = [[1, 8]]
        with self.assertRaisesRegex(ValueError, "selection/spans"):
            profile.compare([before], [after])

    def test_abba_uses_six_process_medians_per_side_not_42_passes(self):
        launches = []
        reports = {"baseline": [], "candidate": []}
        for index, side in enumerate(proximity.ABBA * 3):
            value = fixture(commit=("a" if side == "baseline" else "b") * 40)
            value["processId"] = index + 100
            value["repetitions"] = 7
            prototypes = [row for row in value["observations"] if not row["warmup"]]
            value["observations"] = [
                {**row, "iteration": iteration, "warmup": iteration == 0,
                 "elapsedUs": (100 if side == "baseline" else 50) if iteration < 7 else 10000}
                for iteration in range(8) for row in prototypes
            ]
            reports[side].append(value)
            launches.append({"side": side, "report": value, "reportPath": f"{index}/report.json", "reportSha256": f"{index:064x}"})
        summary = profile.compare(reports["baseline"], reports["candidate"])
        query = proximity.QUERIES[0]
        self.assertEqual(summary["baseline"]["highlight"][query]["completePassUs"]["count"], 42)
        self.assertEqual(summary["baseline"]["highlight"][query]["processMedianUs"]["count"], 6)
        self.assertEqual(summary["highlightChanges"][query]["p50ChangePercent"], -50)
        blocks = proximity.summarize_abba(launches)
        self.assertEqual(len(blocks["processes"]), 12)
        self.assertEqual(len(blocks["blocks"]), 3)
        self.assertEqual(len(blocks["adjacentPairs"]), 6)
        self.assertEqual([row["order"] for row in blocks["adjacentPairs"]], ["AB", "BA"] * 3)
        self.assertTrue(all(row["queries"][query]["changePercent"] == -50 for row in blocks["adjacentPairs"]))
        with self.assertRaises(ValueError):
            proximity.summarize_abba(launches[:-1])
        changed = copy.deepcopy(launches)
        changed[0]["side"] = "candidate"
        with self.assertRaises(ValueError):
            proximity.summarize_abba(changed)


if __name__ == "__main__":
    unittest.main()

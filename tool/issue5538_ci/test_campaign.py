"""Contracts that keep multi-corpus results attributable and reproducible."""

import argparse
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import pypdf

from tool.issue5538_ci import prepare_corpus as corpus
from tool.issue5538_ci import run_campaign as campaign


def plan_value():
    return {"schemaVersion": 1, "baseline": "a" * 40, "candidate": "b" * 40,
            "launchesPerBlock": 3, "repetitions": 5, "cases": ["real-book"],
            "cacheStates": ["cold", "warm"], "scenarios": ["pdf-reader", "pdf-book-view"]}


class PlanTests(unittest.TestCase):
    def test_rejects_moving_refs_invalid_counts_and_unsafe_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "plan.json"
            path.write_text(json.dumps(plan_value()))
            self.assertEqual(campaign.read_plan(path), plan_value())
            for key, value in (("baseline", "dev"), ("candidate", "b" * 39), ("schemaVersion", True),
                               ("launchesPerBlock", 0), ("repetitions", True), ("cases", ["../outside"]),
                               ("cases", [{}]), ("cases", ["real-book", "real-book"]),
                               ("scenarios", ["all"]), ("cacheStates", ["cold", "cold"])):
                invalid = {**plan_value(), key: value}
                path.write_text(json.dumps(invalid))
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    campaign.read_plan(path)

    def test_balances_first_source_within_and_between_conditions(self):
        self.assertEqual(sum((campaign.block_order(0, index) for index in range(2)), ()),
                         ("baseline", "candidate", "candidate", "baseline"))
        self.assertEqual(sum((campaign.block_order(1, index) for index in range(2)), ()),
                         ("candidate", "baseline", "baseline", "candidate"))

    def test_summary_uses_process_clusters_and_excludes_warmup_and_idle_duration(self):
        blocks = []
        for _ in range(2):
            reports = {}
            for role, duration in (("baseline", 10), ("candidate", 8)):
                samples = [{"launchIndex": launch, "warmup": False, "status": "passed", "durationMs": duration}
                           for launch in range(3) for _ in range(5)]
                samples += [{"launchIndex": launch, "warmup": True, "status": "passed", "durationMs": 9999}
                            for launch in range(3)]
                reports[role] = {"scenarios": [
                    {"name": "open", "status": "passed", "samples": samples},
                    {"name": "idle", "status": "passed", "measurementKind": "resource-observation", "samples": samples},
                ]}
            blocks.append(reports)
        summary = campaign.latency_summary(blocks)
        self.assertEqual(set(summary), {"open"})
        self.assertEqual(summary["open"]["baseline"]["observations"], 30)
        self.assertEqual(summary["open"]["baseline"]["processLaunches"], 6)
        self.assertEqual(summary["open"]["medianDeltaMs"], -2)
        self.assertEqual(summary["open"]["medianDeltaPercent"], -20)
        self.assertEqual(summary["open"]["exploratoryClusterBootstrap95Ms"], [-2, -2])
        blocks[0]["candidate"]["scenarios"][0]["samples"][0]["durationMs"] = float("nan")
        with self.assertRaisesRegex(ValueError, "duration"):
            campaign.latency_summary(blocks)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "repo"
        self.source.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.source)], check=True)
        self.git("config", "user.name", "Campaign contract fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        for name in (*campaign.pair.HARNESS_PATHS, *campaign.pair.DEPENDENCY_PATHS):
            path = self.source / name
            if "." not in path.name:
                path /= "fixture.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("identical harness or lock\n")
        application = self.source / "lib/application.txt"
        application.write_text("baseline\n")
        self.baseline = self.commit("baseline")
        application.write_text("candidate\n")
        self.candidate = self.commit("candidate")
        self.cases = self.root / "corpus"
        self.cases.mkdir()
        self.case = {"schemaVersion": 1, "source": {"kind": "generated", "pageCount": 17, "mixedPageSizes": True},
                     "navigation": {"initialPage": 1, "forwardPage": 12, "backwardPage": 2, "zoomFactor": 2.0}}
        for name in ("real-a", "real-b"):
            campaign.pair.write_json(self.cases / f"{name}.json", self.case)
        self.plan = {**plan_value(), "baseline": self.baseline, "candidate": self.candidate,
                     "cases": ["real-a", "real-b"], "cacheStates": ["cold"], "scenarios": ["pdf-reader"]}
        self.plan_path = self.root / "plan.json"
        campaign.pair.write_json(self.plan_path, self.plan)
        self.args = argparse.Namespace(source=self.source, output=self.root / "output", corpus=self.cases,
            plan=self.plan_path, mode="profile", expected_engine_sha256="", display_mode="desktop")
        self.invocations = []
        self.preparations = []

    def git(self, *arguments):
        return campaign.pair.git(self.source, *arguments)

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def logged(self, command, checkout, log):
        if command[1] == "tool/perf_probe/prepare_pdf_case.dart":
            campaign.pair.write_json(Path(command[3]), self.case)
        elif "--pdf-case" in command:
            self.invocations.append(checkout)
            destination = Path(command[command.index("--output") + 1])
            destination.mkdir()
            case = campaign.pair.read_json(Path(command[command.index("--pdf-case") + 1]))
            campaign.pair.write_json(destination / "pdf-case.json", case)
            comparison = copy.deepcopy(case)
            comparison["navigation"].update(zoomPage=2, zoomAnchor="pageCenter")
            report = {"metadata": {"viewport": {"width": 1200, "height": 800, "devicePixelRatio": 1},
                                   "settings": {"pdfReaderCase": comparison, "pdfPageCount": 17}},
                      "scenarios": [{"name": "open", "status": "passed", "samples": [
                          {"status": "passed", "warmup": False, "launchIndex": index, "durationMs": 10}
                          for index in range(3)]}]}
            campaign.pair.write_json(destination / "report.json", report)
            (destination / "data").mkdir()
            (destination / "data/copy.pdf").write_bytes(b"temporary owned fixture")
        elif command[0] == "flutter":
            self.preparations.append((checkout, command[1]))
        return 0.01

    def execute(self, engine_failure=None):
        with patch.object(campaign.platform, "system", return_value="Linux"), \
                patch.object(campaign.shutil, "which", side_effect=lambda name: name), \
                patch.object(campaign.pair, "sdk_identity", return_value={"frameworkRevision": "a" * 40}), \
                patch.object(campaign.pair, "run_logged", side_effect=self.logged), \
                patch.object(campaign.pair, "verify_posix_engines", side_effect=engine_failure) as engines, \
                patch.object(campaign.pair, "validate_reports", return_value={"comparable": True}) as comparisons, \
                contextlib.redirect_stdout(io.StringIO()):
            campaign.campaign(self.args)
        return engines.call_count, comparisons.call_count

    def test_reuses_two_checkouts_and_preserves_every_balanced_block(self):
        original = self.git("worktree", "list", "--porcelain")
        self.assertEqual(self.execute(), (4, 4))
        self.assertEqual([path.name for path in self.invocations],
                         ["baseline", "candidate", "candidate", "baseline", "candidate", "baseline", "baseline", "candidate"])
        self.assertEqual(len(set(self.invocations)), 2)
        self.assertEqual(len(self.preparations), 6)
        receipt = campaign.pair.read_json(self.args.output / "campaign.json")
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(len(receipt["conditions"]), 2)
        self.assertTrue(all(condition["status"] == "passed" for condition in receipt["conditions"]))
        self.assertEqual(len(list((self.args.output / "conditions").glob("*/block-*/baseline/report.json"))), 4)
        self.assertEqual(list((self.args.output / "conditions").glob("*/block-*/*/data")), [])
        self.assertEqual(self.git("worktree", "list", "--porcelain"), original)

    def test_engine_failure_keeps_prior_conditions_and_partial_current_block(self):
        original = self.git("worktree", "list", "--porcelain")
        with self.assertRaisesRegex(ValueError, "injected engine mismatch"):
            self.execute([None, None, ValueError("injected engine mismatch")])
        receipt = campaign.pair.read_json(self.args.output / "campaign.json")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["conditions"][0]["status"], "passed")
        self.assertTrue((self.args.output / "results/baseline/report.json").exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), original)

    def test_rejects_dirty_orchestrator_before_creating_outputs(self):
        (self.source / "unexpected.txt").write_text("untracked")
        with self.assertRaisesRegex(ValueError, "dirty"):
            self.execute()
        self.assertFalse(self.args.output.exists())

    def test_output_cannot_contain_or_overlap_source_or_corpus(self):
        for output in (self.source / "results", self.cases / "results", self.root):
            self.args.output = output
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "outside"):
                self.execute()


class CorpusTests(unittest.TestCase):
    def test_catalog_is_pinned_and_copy_refuses_modified_bytes(self):
        catalog = corpus.validate_catalog(json.loads(corpus.CATALOG.read_text()))
        self.assertEqual(len(catalog["documents"]), 5)
        invalid = copy.deepcopy(catalog)
        invalid["documents"][0]["sha256"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "pinned"):
            corpus.validate_catalog(invalid)
        for invalid in ({**catalog, "schemaVersion": True}, {"schemaVersion": 1, "documents": ["invalid"]}):
            with self.assertRaises(ValueError):
                corpus.validate_catalog(invalid)
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "book.pdf"
            source.write_bytes(b"%PDF-1.7\nreviewed bytes")
            record = {"id": "book", "sizeBytes": source.stat().st_size, "sha256": corpus.digest(source)}
            corpus.download(record, Path(temp) / "copied.pdf", Path(temp))
            source.write_bytes(b"%PDF-1.7\nmodified bytes")
            with self.assertRaisesRegex(ValueError, "pin"):
                corpus.download(record, Path(temp) / "second.pdf", Path(temp))
            self.assertFalse((Path(temp) / "second.pdf").exists())

    def test_rate_limit_stops_without_rotating_sources_or_retrying(self):
        record = {"id": "book", "sourceUrl": "https://example.invalid/book.pdf", "sizeBytes": 10}
        error = urllib.error.HTTPError(record["sourceUrl"], 429, "rate limited", {"Retry-After": "120"}, None)
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(corpus.urllib.request, "urlopen", side_effect=error) as request, \
                patch.object(corpus.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                corpus.download(record, Path(temp) / "book.pdf")
            self.assertEqual(request.call_count, 1)
            sleep.assert_not_called()

    def test_component_selection_records_omitted_sources_without_opening_them(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "selected.pdf"
            writer = pypdf.PdfWriter()
            writer.add_blank_page(width=300, height=400)
            writer.write(source)
            item = {**json.loads(corpus.CATALOG.read_text())["documents"][0], "id": "selected",
                    "sizeBytes": source.stat().st_size, "sha256": corpus.digest(source), "pageCount": 1}
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"schemaVersion": 1, "documents": [item, {**item, "id": "not-selected"}]}))
            with contextlib.redirect_stdout(io.StringIO()):
                corpus.prepare(argparse.Namespace(catalog=catalog, output=root / "result", input_directory=root,
                    document=["selected"], skip_derived_stress=True, qpdf=None))
            receipt = json.loads((root / "result/corpus.json").read_text())
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["requestedDocuments"], ["selected"])
            self.assertEqual(receipt["unpreparedDocuments"], ["not-selected"])
            self.assertFalse(receipt["derivedStressPrepared"])
            self.assertEqual([document["id"] for document in receipt["documents"]], ["selected"])

    def test_derived_pages_repeat_geometry_without_claiming_a_new_real_book(self):
        with tempfile.TemporaryDirectory() as temp:
            source, derived = (Path(temp) / name for name in ("source.pdf", "derived.pdf"))
            writer = pypdf.PdfWriter()
            writer.add_blank_page(width=300, height=400)
            writer.add_blank_page(width=600, height=800)
            writer.write(source)
            corpus.derive_repeated_pages(source, derived, 5)
            reader = pypdf.PdfReader(derived)
            self.assertEqual([float(page.cropbox.width) for page in reader.pages], [300, 600, 300, 600, 300])
            self.assertIn("not a 5-page original book", reader.metadata.subject)
            inspected = corpus.describe_pdf(derived)
            self.assertEqual(inspected["pageCount"], 5)
            self.assertEqual(inspected["textInspection"]["characters"], 0)


if __name__ == "__main__":
    unittest.main()

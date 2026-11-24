import copy
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from tool.issue5538_ci import wrap_search_run as host
from tool.issue5538_ci.wrap_search_run import extract_regular_archive, instrument
from tool.issue5538_ci.wrap_search_validate import expected_order, summarize, validate_report


def report(variant='control', order='page1'):
    page_count, target = 8, 6
    pages = expected_order(page_count, target, order)
    ranges = [{'pageNumber': p, 'start': p - 1, 'end': p} for p in range(1, page_count + 1)]
    base = {'schemaVersion': 1, 'status': 'passed', 'executionMode': 'flutter-profile',
            'component': 'actual-centered-search-session-wrap-insertion',
            'variant': variant, 'scanCondition': order, 'pageCount': page_count, 'targetPage': target,
            'warmup': 1, 'repetitions': 5 if variant == 'control' else 1,
            'sourceCommit': 'a' * 40, 'host': 'fixed', 'dartVersion': '3.13.5',
            'inputSha256': 'b' * 64, 'inputBytes': 100, 'pdfiumSha256': 'c' * 64,
            'searchEngineSha256': 'd' * 64, 'queryDescriptorsSha256': 'e' * 64,
            'pageTextHashesSha256': 'f' * 64, 'referenceSha256': '0' * 64,
            'queries': [{'id': 'app-simple', 'builder': 'PdfBookSearchView.buildSimpleSearchPattern'},
                        {'id': 'app-user-regex', 'builder': 'PdfUserRegexPattern.tryParse'}],
            'observations': [], 'correctness': []}
    for query in ('app-simple', 'app-user-regex'):
        row = {'queryId': query, 'scanOrder': pages, 'redirectCount': 1 if order == 'redirect' else 0,
               'resultCount': page_count, 'resultSha256': '1' * 64, 'pageRanges': ranges,
               'currentIndex': None, 'selectedKey': None, 'correctnessOnly': False,
               'sessionElapsedUs': 1000, 'firstTargetMatchNotificationUs': 600,
               'firstTargetMatchPage': target, 'insertionProbes': [], 'retainedSnapshots': [],
               'timingClass': 'unmodified-control' if variant == 'control' else 'instrumented-diagnostic'}
        if variant == 'counts':
            scanned = []
            for page in pages:
                before = len(scanned)
                offset = sum(p < page for p in scanned)
                copied = offset != before
                row['insertionProbes'].append({'pageNumber': page, 'beforeLength': before,
                    'addedMatches': 1, 'insertAt': offset, 'afterLength': before + 1,
                    'copiedExistingReferences': before if copied else 0,
                    'newListReferences': before + 1 if copied else 0,
                    'allocatedNewResultList': copied, 'currentIndexBefore': None,
                    'currentIndexAfter': None, 'instrumentedInsertionUs': 1})
                scanned.append(page)
        for warmup, index in [(True, 0), *[(False, x) for x in range(base['repetitions'])]]:
            base['observations'].append({**copy.deepcopy(row), 'warmup': warmup, 'iteration': index})
        base['correctness'].append({**row, 'correctnessOnly': True, 'currentIndex': 0,
            'selectedKey': '1:0:1', 'retainedSnapshots': [{'length': 1, 'sha256Before': '2' * 64,
                                                         'sha256After': '2' * 64}]})
    return base


class EvidenceTests(unittest.TestCase):
    def test_valid_control_and_counts_for_all_real_scan_shapes(self):
        for variant in ('control', 'counts'):
            for order in ('page1', 'start75', 'redirect'):
                self.assertIs(validate_report(value := report(variant, order)), value)

    def test_counter_proof_rejects_hidden_full_copy(self):
        value = report('counts', 'start75')
        value['observations'][0]['insertionProbes'][3]['copiedExistingReferences'] = 0
        with self.assertRaisesRegex(ValueError, 'count/offset'):
            validate_report(value)

    def test_control_cannot_contain_instrumented_times(self):
        value = report()
        value['observations'][0]['timingClass'] = 'instrumented-diagnostic'
        with self.assertRaisesRegex(ValueError, 'Control contains'):
            validate_report(value)

    def test_duplicate_dependent_pass_is_not_an_independent_sample(self):
        value = report()
        value['observations'].append(copy.deepcopy(value['observations'][0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate or missing'):
            validate_report(value)

    def test_retained_snapshot_drift_is_rejected(self):
        value = report('counts', 'redirect')
        value['correctness'][0]['retainedSnapshots'][0]['sha256After'] = '3' * 64
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            validate_report(value)

    def test_unselected_correctness_does_not_pass_as_current_index_parity(self):
        value = report()
        value['correctness'][0]['currentIndex'] = None
        with self.assertRaisesRegex(ValueError, 'selection was not exercised'):
            validate_report(value)

    def test_partial_matrix_is_explicit_and_cannot_be_marked_complete(self):
        values = [{'block': 0, 'path': 'one-control', 'report': report()}]
        partial = summarize(values, complete=False)
        self.assertFalse(partial['declaredScopeComplete'])
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            summarize(values, complete=True)

    def test_same_process_cannot_be_counted_twice(self):
        value = {'block': 0, 'path': 'one-control', 'report': report()}
        with self.assertRaisesRegex(ValueError, 'Repeated independent'):
            summarize([value, value], complete=False)

    def test_cross_process_geometry_identity_is_required(self):
        first, second = report(), report(order='start75')
        second['referenceSha256'] = '9' * 64
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            summarize([{'block': 0, 'path': 'one', 'report': first},
                       {'block': 0, 'path': 'two', 'report': second}], complete=False)


class InstrumentationTests(unittest.TestCase):
    def test_only_observers_are_inserted_around_unchanged_copy(self):
        source = ("import 'dart:collection';\n"
                  '        final pageIndex = page.pageNumber - 1;\n'
                  '        textMatches = [...textMatches.take(insertAt), ...pageMatches, ...textMatches.skip(insertAt)];\n'
                  '        _matches = _MatchSnapshot(textMatches);\n')
        patched = instrument(source, 'file:///owned/wrap_search_metrics.dart')
        self.assertIn('Issue5538WrapMetrics.begin()', patched)
        self.assertIn('Issue5538WrapMetrics.finish(', patched)
        self.assertIn(source.splitlines()[2], patched)
        self.assertEqual(patched.count('_matches = _MatchSnapshot(textMatches);'), 1)

    def test_source_drift_fails_without_partial_patch(self):
        with self.assertRaisesRegex(ValueError, 'anchor changed'):
            instrument("import 'dart:collection';", 'file:///owned/metrics.dart')


class ArchiveCompatibilityTests(unittest.TestCase):
    def test_extracts_nested_regular_files_without_new_python_filter_api(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'input.tar'
            with tarfile.open(archive, 'w') as output:
                member = tarfile.TarInfo('lib/src/search.dart')
                member.size = 7
                output.addfile(member, io.BytesIO(b'content'))
            with tarfile.open(archive) as source:
                extract_regular_archive(source, Path(directory) / 'viewer')
            self.assertEqual((Path(directory) / 'viewer/lib/src/search.dart').read_bytes(), b'content')

    def test_rejects_escapes_links_and_devices_before_creating_destination(self):
        for name, kind in (('../outside', tarfile.REGTYPE), ('/outside', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('link', tarfile.LNKTYPE),
                           ('device', tarfile.CHRTYPE), ('fifo', tarfile.FIFOTYPE)):
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / 'input.tar'
                with tarfile.open(archive, 'w') as output:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = '../outside'
                    output.addfile(member)
                destination = Path(directory) / 'viewer'
                with tarfile.open(archive) as source, self.assertRaisesRegex(ValueError, 'unsafe'):
                    extract_regular_archive(source, destination)
                self.assertFalse(destination.exists())


class CompilerReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wrap receipt ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / 'source'
        self.directory = self.root / 'evidence'
        self.directory.mkdir()
        self.viewer = self.directory / 'viewer'
        self.config = self.checkout / '.dart_tool/package_config.json'
        self.config.parent.mkdir(parents=True)
        self.original = {'configVersion': 2, 'packages': [
            {'name': 'otzaria', 'rootUri': '../', 'packageUri': 'lib/', 'languageVersion': '3.10'},
            {'name': 'pdfrx', 'rootUri': '../../pub-cache/pdfrx-2.13.1', 'packageUri': 'lib/', 'languageVersion': '3.10'},
            {'name': 'pdfrx_engine', 'rootUri': '../../pub-cache/engine-0.9.3', 'packageUri': 'lib/', 'languageVersion': '3.10'},
        ]}
        self.config.write_text(json.dumps(self.original) + '\n')
        self.graph = self.config.with_name('package_graph.json')
        self.graph.write_bytes(b'{"packages": [{"name": "pdfrx", "version": "2.13.1"}]}\n')
        self.overlay = self.directory / 'packages.json'
        mapping = host.normalized_config(self.original, self.config)
        mapping['packages'][1]['rootUri'] = self.viewer.as_uri() + '/'
        host.write(self.overlay, mapping)
        self.required = [self.checkout / host.TARGET, self.checkout / host.METRICS,
                         self.checkout / host.APP[0], self.viewer / host.SEARCHER]
        for path in self.required:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('declared source')

    def install(self):
        return host.install_project_overlay(self.checkout, self.viewer, self.overlay)

    def compiler(self, name='build-key', *, inputs=None, stamp_inputs=None, output=None):
        directory = self.checkout / '.dart_tool/flutter_build' / name
        directory.mkdir(parents=True)
        kernel = directory / 'app.dill'
        kernel.write_bytes(b'compiled kernel output')
        inputs = self.required if inputs is None else inputs
        def encoded(path):
            return str(path).replace('\\', '\\\\').replace(' ', '\\ ')
        depfile = directory / 'kernel_snapshot_program.d'
        depfile.write_text(encoded(output or kernel) + ': ' + ' '.join(map(encoded, inputs)))
        host.write(directory / 'kernel_snapshot_program.stamp', {
            'inputs': list(map(str, [*inputs, self.config] if stamp_inputs is None else stamp_inputs)),
            'outputs': [str(kernel)],
        })
        return depfile

    def test_installs_only_declared_viewer_root_and_preserves_official_bytes(self):
        before_config, before_graph = self.config.read_bytes(), self.graph.read_bytes()
        receipt = self.install()
        self.assertEqual(self.config.read_bytes(), self.overlay.read_bytes())
        self.assertEqual(self.graph.read_bytes(), before_graph)
        self.assertEqual((self.directory / 'official-package-config.json').read_bytes(), before_config)
        self.assertEqual((self.directory / 'official-package-graph.json').read_bytes(), before_graph)
        self.assertEqual(receipt['changedPackageRoots'], ['pdfrx'])
        host.verify_installed_overlay(self.checkout, self.overlay)

    def test_another_package_root_change_is_rejected_before_install(self):
        original = self.config.read_bytes()
        mapping = json.loads(self.overlay.read_text())
        mapping['packages'][2]['rootUri'] = 'file:///foreign-engine/'
        host.write(self.overlay, mapping)
        with self.assertRaisesRegex(ValueError, 'more than the declared viewer root'):
            self.install()
        self.assertEqual(self.config.read_bytes(), original)

    def test_any_original_or_installed_resolution_drift_rejects_reuse(self):
        self.install()
        for path in [self.config, self.graph, self.overlay,
                     self.directory / 'official-package-config.json',
                     self.directory / 'official-package-graph.json']:
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b' ')
                with self.assertRaisesRegex(ValueError, 'resolution changed'):
                    host.verify_installed_overlay(self.checkout, self.overlay)
                path.write_bytes(original)
        host.verify_installed_overlay(self.checkout, self.overlay)

    def test_missing_official_graph_cannot_be_invented(self):
        original = self.config.read_bytes()
        self.graph.unlink()
        with self.assertRaisesRegex(ValueError, 'official pub resolution'):
            self.install()
        self.assertEqual(self.config.read_bytes(), original)

    def test_official_kernel_record_proves_exact_sources_and_installed_config(self):
        self.install()
        depfile = self.compiler()
        receipt = host.compiler_inputs(self.checkout, self.viewer, self.directory)
        self.assertEqual(receipt['depfile']['path'], str(depfile))
        actual = {row['canonicalPath'] for row in receipt['inputs']}
        self.assertTrue({str(p) for p in self.required + [self.config]} <= actual)
        self.assertEqual(receipt['installedPackageConfig']['sha256'], host.digest(self.overlay))
        diagnostics = json.loads((self.directory / 'compiler-input-diagnostics.json').read_text())
        self.assertEqual(diagnostics['matchingDepfiles'], 1)
        self.assertTrue(diagnostics['candidates'][0]['stampIncludesInstalledPackageConfig'])

    def test_original_pub_viewer_cannot_impersonate_private_overlay(self):
        self.install()
        pub_viewer = self.root / 'pub-cache/pdfrx-2.13.1' / host.SEARCHER
        pub_viewer.parent.mkdir(parents=True)
        pub_viewer.write_text(self.required[-1].read_text())
        self.compiler(inputs=[*self.required[:-1], pub_viewer])
        with self.assertRaisesRegex(ValueError, 'Need one compiler depfile'):
            host.compiler_inputs(self.checkout, self.viewer, self.directory)
        diagnostics = json.loads((self.directory / 'compiler-input-diagnostics.json').read_text())
        self.assertEqual(diagnostics['candidates'][0]['missingRequiredInputs'], [str(self.required[-1])])
        self.assertEqual(diagnostics['candidates'][0]['searcherInputs'], [str(pub_viewer)])

    def test_compiler_stamp_requires_the_actual_project_package_config(self):
        self.install()
        self.compiler(stamp_inputs=[*self.required, self.overlay])
        with self.assertRaisesRegex(ValueError, 'Need one compiler depfile'):
            host.compiler_inputs(self.checkout, self.viewer, self.directory)
        row = json.loads((self.directory / 'compiler-input-diagnostics.json').read_text())['candidates'][0]
        self.assertFalse(row['stampIncludesInstalledPackageConfig'])

    def test_unrelated_kernel_output_and_ambiguous_builds_are_rejected(self):
        self.install()
        bad = self.compiler(output=self.root / 'unrelated.dill')
        with self.assertRaisesRegex(ValueError, 'Need one compiler depfile'):
            host.compiler_inputs(self.checkout, self.viewer)
        bad.unlink()
        self.compiler('good-first')
        self.compiler('good-second')
        with self.assertRaisesRegex(ValueError, 'Need one compiler depfile'):
            host.compiler_inputs(self.checkout, self.viewer)

    def test_raw_compiler_records_survive_failure_and_existing_artifact_globs(self):
        depfile = self.compiler()
        depfile.write_bytes(b'invalid raw depfile retained unchanged\n')
        host.preserve_compiler_records(self.checkout, self.directory)
        records = json.loads((self.directory / 'compiler-records.json').read_text())['records']
        self.assertEqual(len(records), 2)
        for record in records:
            copied = Path(record['copy']['path'])
            self.assertEqual(copied.read_bytes(), Path(record['original']['path']).read_bytes())
            self.assertTrue(copied.match('*.log'))
        with self.assertRaisesRegex(ValueError, 'Need one compiler depfile'):
            host.compiler_inputs(self.checkout, self.viewer, self.directory)
        row = json.loads((self.directory / 'compiler-input-diagnostics.json').read_text())['candidates'][0]
        self.assertEqual(row['error'], 'Invalid compiler depfile')


if __name__ == '__main__':
    unittest.main()

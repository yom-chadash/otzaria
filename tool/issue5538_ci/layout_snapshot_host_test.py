"""Integrity contracts for the M02 observer; no Flutter compiler is launched."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader

import run_layout_snapshot_probe as host


class HostContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_synthetic_pdf_is_independently_parseable_at_all_sizes(self):
        for count in (100, 1000, 10000):
            path = self.root / f'{count}.pdf'
            path.write_bytes(host.synthetic_pdf(count))
            reader = PdfReader(path)
            self.assertEqual(len(reader.pages), count)
            for index in (0, count // 2, count - 1):
                page = reader.pages[index]
                self.assertEqual((float(page.mediabox.width), float(page.mediabox.height)),
                                 (400 + index % 3, 600 + index % 5))
                self.assertEqual(page.get_contents().get_data(), b'0 0 0 rg 20 20 40 30 re f\n')

    def test_instrumentation_is_explicit_and_fails_on_source_drift(self):
        source = "import 'dart:ui';\n  bool matches(List<Rect> rects) {\n      if (rects[i] != _pageRects[i]) return false;\n    _root = pageRects.isEmpty"
        changed = host.instrument_index(source, 'file:///observer/layout_snapshot_metrics.dart')
        self.assertEqual(changed.count('Issue5538LayoutCounters.rectChecks++;'), 1)
        self.assertEqual(changed.count('Issue5538LayoutCounters.matchCalls++;'), 1)
        self.assertIn("import 'file:///observer/layout_snapshot_metrics.dart';", changed)
        self.assertIn('if (rects[i] != _pageRects[i]) return false;', changed)
        with self.assertRaisesRegex(ValueError, 'anchor changed'):
            host.instrument_index(source.replace('rects[i] !=', 'rects[i] =='), 'file:///observer/metrics.dart')
        with self.assertRaisesRegex(ValueError, 'Unsafe observer'):
            host.instrument_index(source, 'https://arbitrary.example/metrics.dart')

    def test_packages_are_resolved_without_mutating_the_official_file(self):
        project = self.root / 'project'
        (project / '.dart_tool').mkdir(parents=True)
        package = self.root / 'dependency'
        package.mkdir()
        path = project / '.dart_tool/package_config.json'
        original = {'configVersion': 2, 'packages': [{'name': 'sample', 'rootUri': '../../dependency', 'packageUri': 'lib/'}]}
        host.save(path, original)
        before = path.read_bytes()
        value = host.read_packages(path)
        self.assertEqual(value['packages'][0]['rootUri'], package.as_uri() + '/')
        self.assertEqual(path.read_bytes(), before)

    def test_corpus_alias_and_escape_fail_before_use(self):
        corpus = self.root / 'corpus'
        corpus.mkdir()
        outside = self.root / 'outside.pdf'
        outside.write_bytes(host.synthetic_pdf(3))
        (corpus / 'alias.pdf').symlink_to(outside)
        checksum = host.digest(outside)
        for relative in ('../outside.pdf', 'alias.pdf'):
            with self.subTest(relative=relative), patch.dict(host.CORPUS, {'paper': (checksum, 3)}, clear=True):
                manifest = corpus / 'corpus.json'
                host.save(manifest, {'documents': [{'id': 'paper', 'path': relative, 'sha256': checksum,
                    'pageCount': 3, 'sizeBytes': outside.stat().st_size}]})
                output = self.root / ('output-' + str(len(list(self.root.glob('output-*')))))
                output.mkdir()
                with self.assertRaisesRegex(ValueError, 'escapes or aliases'):
                    host.documents(manifest, output)

    def _package_inputs(self):
        project = self.root / 'project'
        official = project / '.dart_tool'
        private = self.root / 'overlay'
        official.mkdir(parents=True)
        private.mkdir()
        config = official / 'package_config.json'
        copied_config = private / 'packages.json'
        original_viewer = self.root / 'official-viewer'
        original_viewer.mkdir()
        private_viewer = private / 'viewer'
        private_viewer.mkdir()
        other = self.root / 'other-package'
        other.mkdir()
        (project / 'pubspec.yaml').write_text('name: app\n')
        (project / 'pubspec.lock').write_text('unchanged official lock\n')
        host.save(config, {'configVersion': 2, 'packages': [
            {'name': 'app', 'rootUri': '../', 'packageUri': 'lib/', 'languageVersion': '3.9'},
            {'name': 'pdfrx', 'rootUri': '../../official-viewer', 'packageUri': 'lib/', 'languageVersion': '3.9'},
            {'name': 'other', 'rootUri': '../../other-package', 'packageUri': 'lib/', 'languageVersion': '3.9'},
        ]})
        overlay = host.read_packages(config)
        next(row for row in overlay['packages'] if row['name'] == 'pdfrx')['rootUri'] = private_viewer.as_uri() + '/'
        host.save(copied_config, overlay)
        graph = official / 'package_graph.json'
        graph.write_bytes(b'{ "roots": ["app"], "packages": [], "configVersion": 1 }\n')
        return project, config, copied_config, graph

    def test_official_graph_is_copied_verbatim_next_to_private_packages(self):
        project, config, private, graph = self._package_inputs()
        before = graph.read_bytes()
        receipt = host.copy_package_graph(config, private)
        copied = private.with_name('package_graph.json')
        self.assertEqual(copied.read_bytes(), before)
        self.assertEqual(graph.read_bytes(), before)
        self.assertEqual(receipt['originalPackageGraphPath'], str(graph))
        self.assertEqual(receipt['overlayPackageGraphPath'], str(copied))
        self.assertEqual(receipt['originalPackageGraphSha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(receipt['overlayPackageGraphSha256'], receipt['originalPackageGraphSha256'])

    def test_missing_official_graph_is_not_synthesized(self):
        _, config, private, graph = self._package_inputs()
        graph.unlink()
        with self.assertRaisesRegex(ValueError, 'Official pub package graph'):
            host.copy_package_graph(config, private)
        self.assertFalse(private.with_name('package_graph.json').exists())

    def test_both_original_and_private_graph_drift_are_rejected(self):
        project, config, private, graph = self._package_inputs()
        receipt = {**host.copy_package_graph(config, private),
                   **host.install_project_overlay(project, private.parent / 'viewer', private)}
        host.verify_package_inputs(project, config, receipt)
        for path in (graph, private.with_name('package_graph.json')):
            before = path.read_bytes()
            path.write_bytes(before + b' ')
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'package graph changed'):
                host.verify_package_inputs(project, config, receipt)
            path.write_bytes(before)
        host.verify_package_inputs(project, config, receipt)

    def test_installed_overlay_preserves_official_bytes_and_only_changes_viewer_root(self):
        project, config, private, graph = self._package_inputs()
        original_config, original_graph = config.read_bytes(), graph.read_bytes()
        source_files = {name: (project / name).read_bytes() for name in ('pubspec.yaml', 'pubspec.lock')}
        receipt = {**host.copy_package_graph(config, private),
                   **host.install_project_overlay(project, private.parent / 'viewer', private)}
        self.assertEqual(config.read_bytes(), private.read_bytes())
        self.assertEqual(graph.read_bytes(), original_graph)
        self.assertEqual(Path(receipt['originalPackageConfigBackupPath']).read_bytes(), original_config)
        self.assertEqual(Path(receipt['originalPackageGraphBackupPath']).read_bytes(), original_graph)
        self.assertEqual({name: (project / name).read_bytes() for name in source_files}, source_files)
        self.assertEqual(receipt['changedPackageRoots'], ['pdfrx'])
        self.assertEqual(receipt['installedPackageConfigPath'], str(config))
        installed = host.read_packages(config)
        self.assertEqual(host.package_root(installed, 'app'), project)
        self.assertEqual(host.package_root(installed, 'other'), self.root / 'other-package')
        self.assertEqual(host.package_root(installed, 'pdfrx'), private.parent / 'viewer')
        host.verify_package_inputs(project, config, receipt)

    def test_overlay_cannot_change_another_root_or_language_version(self):
        project, config, private, _ = self._package_inputs()
        original = config.read_bytes()
        initial_overlay = private.read_bytes()
        for name, field, value in [('app', 'rootUri', (self.root / 'other-package').as_uri() + '/'),
                                   ('pdfrx', 'languageVersion', '3.0')]:
            overlay = json.loads(initial_overlay)
            next(row for row in overlay['packages'] if row['name'] == name)[field] = value
            host.save(private, overlay)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'more than the declared viewer root'):
                host.install_project_overlay(project, private.parent / 'viewer', private)
            self.assertEqual(config.read_bytes(), original)
            self.assertFalse((private.parent / 'official-package-config.json').exists())
        private.write_bytes(initial_overlay)

    def test_official_backups_private_mapping_and_installed_mapping_are_immutable(self):
        project, config, private, _ = self._package_inputs()
        receipt = {**host.copy_package_graph(config, private),
                   **host.install_project_overlay(project, private.parent / 'viewer', private)}
        for path in [config, private, Path(receipt['originalPackageConfigBackupPath']),
                     Path(receipt['originalPackageGraphBackupPath'])]:
            before = path.read_bytes()
            path.write_bytes(before + b' ')
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'package mapping changed'):
                host.verify_package_inputs(project, config, receipt)
            path.write_bytes(before)
        host.verify_package_inputs(project, config, receipt)

    def test_official_backups_cannot_be_overwritten_by_a_second_install(self):
        project, config, private, _ = self._package_inputs()
        host.copy_package_graph(config, private)
        receipt = host.install_project_overlay(project, private.parent / 'viewer', private)
        backup = Path(receipt['originalPackageConfigBackupPath'])
        original = backup.read_bytes()
        with self.assertRaisesRegex(ValueError, 'backups must be new'):
            host.install_project_overlay(project, private.parent / 'viewer', private)
        self.assertEqual(backup.read_bytes(), original)

    def test_external_packages_file_is_rejected_before_any_flutter_process(self):
        project, config, private, _ = self._package_inputs()
        receipt = {**host.copy_package_graph(config, private),
                   **host.install_project_overlay(project, private.parent / 'viewer', private)}
        with self.assertRaisesRegex(ValueError, 'project-local package configuration'):
            host.verify_package_inputs(project, private, receipt)
        with patch.object(host.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'project-local package configuration'):
                host.launch(Path('/flutter'), Path('/sdk'), project, private,
                            Path('/shared/target.dart'), {}, self.root / 'launch')
            spawn.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux', 'Linux waitid ownership contract')
    def test_exited_leader_stays_waitable_until_group_cleanup(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        deadline = time.monotonic() + 5
        while host.owned_exit(child) is None:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT).si_pid, child.pid)
        host.close_owned(child)
        self.assertEqual(child.returncode, 0)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux waitid ownership contract')
    def test_external_reap_prevents_group_signalling(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        child.wait(timeout=5)
        with patch.object(host.os, 'killpg') as signal_group:
            with self.assertRaises(ChildProcessError):
                host.close_owned(child)
            signal_group.assert_not_called()

    def _report(self, candidate=False):
        frame = Image.new('RGBA', (400, 500), (255, 255, 255, 255))
        frame.save(self.root / 'viewport.png')
        proof = lambda page: {'page': page, 'text': 'שלום', 'utf16Units': 4,
            'textSha256': hashlib.sha256('שלום'.encode()).hexdigest(), 'charRectCount': 4,
            'allCharRectanglesSha256': 'b' * 64, 'rasterSha256': 'c' * 64, 'rasterWidth': 128, 'rasterHeight': 160}
        states = {name: {'pageCount': 1 if name in ('remove-tail', 'parameters') else 2,
                         'allRectanglesSha256': 'd' * 64, 'allCenterHitsSha256': 'e' * 64}
                  for name in ('initial', 'rotate-first', 'reverse', 'remove-tail', 'parameters')}
        states.update(mutableRectListDetected=True, allInitialPages=[proof(1), proof(2)],
                      initialViewport={'pngPath': 'viewport.png', 'pngSha256': host.digest(self.root / 'viewport.png'),
                       'rgbaSha256': hashlib.sha256(frame.tobytes()).hexdigest(), 'width': 400, 'height': 500})
        for name in ('rotate-first', 'reverse', 'remove-tail', 'parameters'):
            states[name + '-affectedPages'] = [proof(1), proof(1 if name in ('remove-tail', 'parameters') else 2)]
        config = {'source': 'a' * 40, 'appHash': 'f' * 64, 'candidate': candidate,
                  'documents': [{'id': 'paper', 'pageCount': 2, 'kind': 'real-pdf'}]}
        rows = [{'kind': 'synthetic-metadata-helper', 'pages': count, 'cacheHits': 200,
                 'firstBuildSizeGetters': count * 2, 'parameterChangeSizeGetters': count * 2,
                 'cacheHitSizeGetters': 0 if candidate else 400 * count} for count in (100, 1000, 10000)]
        for phase in ('pan', 'zoom'):
            rows.append({'kind': 'native-viewer-stable-matrix', 'id': 'paper', 'pages': 2, 'phase': phase,
                         'matrixUpdates': 100, 'matrixChanges': 100, 'layoutCallbacks': 100,
                         'counts': {'indexBuilds': 0, 'indexBuildRects': 0, 'matchCalls': 0 if candidate else 100,
                                    'rectChecks': 0 if candidate else 200}})
        value = {**{key: config[key] for key in ('source', 'appHash', 'candidate')}, 'status': 'completed',
                 'executionMode': 'flutter-test-debug-jit', 'instrumented': True, 'latencyClaim': False,
                 'counts': rows, 'parity': {'generalMutablePageSemantics': True, 'paper': states},
                 'completedTests': [f'synthetic metadata getter work {count}' for count in (100, 1000, 10000)] +
                    ['general mutable page metadata still invalidates', 'native layout paper']}
        return config, value

    def test_native_validation_does_not_mutate_raw_report(self):
        config, value = self._report()
        original = copy.deepcopy(value)
        host.validate_result(self.root, value, config)
        self.assertEqual(value, original)
        host.validate_result(self.root, value, config)

    def test_counts_and_native_coverage_are_not_optional(self):
        for change, message in ((lambda v: v['counts'][0].update(cacheHitSizeGetters=0), 'getter counts'),
                                (lambda v: v['counts'][-1]['counts'].update(rectChecks=0), 'rectangle validation'),
                                (lambda v: v['parity']['paper']['allInitialPages'].pop(), 'every real page'),
                                (lambda v: v['parity']['paper'].update(mutableRectListDetected=False), 'Mutable rectangle')):
            config, value = self._report()
            change(value)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                host.validate_result(self.root, value, config)

    def test_candidate_cannot_keep_linear_rectangle_checks(self):
        config, value = self._report(candidate=True)
        host.validate_result(self.root, value, config)
        value['counts'][-1]['counts'].update(matchCalls=1, rectChecks=2)
        with self.assertRaisesRegex(ValueError, 'rectangle validation'):
            host.validate_result(self.root, value, config)

    def test_pan_and_zoom_must_each_change_the_matrix(self):
        for phase in ('pan', 'zoom'):
            config, value = self._report()
            next(row for row in value['counts'] if row.get('phase') == phase)['matrixChanges'] = 0
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, 'did no work'):
                host.validate_result(self.root, value, config)

    def test_missing_phase_or_checkpoint_cannot_pass(self):
        config, value = self._report()
        value['counts'].pop()
        with self.assertRaisesRegex(ValueError, 'pan/zoom counts'):
            host.validate_result(self.root, value, config)
        config, value = self._report()
        value['completedTests'].pop()
        with self.assertRaisesRegex(ValueError, 'checkpoints are incomplete'):
            host.validate_result(self.root, value, config)


if __name__ == '__main__':
    unittest.main()

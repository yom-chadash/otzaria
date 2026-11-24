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

"""Run the upstream native, viewer and browser suites outside the application SDK."""

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import traceback

import internal_upstream_run as upstream


def dart_execution(text):
    events = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    started = {e['test']['id']: e['test'] for e in events if e.get('type') == 'testStart'}
    completed = [e for e in events if e.get('type') == 'testDone' and not e.get('hidden', False)]
    skipped = []
    failed = []
    for event in completed:
        test = started.get(event.get('testID'), {})
        if event.get('skipped', False):
            skipped.append({'id': event.get('testID'), 'name': test.get('name'),
                            'reason': test.get('metadata', {}).get('skipReason')})
        elif event.get('result') != 'success':
            failed.append({'id': event.get('testID'), 'name': test.get('name'), 'result': event.get('result')})
    return {'executed': sum(not e.get('skipped', False) for e in completed),
            'skipped': len(skipped), 'skippedTests': skipped, 'failedTests': failed}


def viewer_pdfium(source, system=None):
    """Reuse the actual native-test module for upstream's explicit PDFIUM_PATH gate."""
    name = {'Linux': 'libpdfium.so', 'Darwin': 'libpdfium.dylib',
            'Windows': 'pdfium.dll'}[system or platform.system()]
    candidates = sorted(p for p in (source / '.dart_tool/hooks_runner/shared/pdfium_dart/build').rglob(name)
                        if p.is_file() and not p.is_symlink())
    upstream.require(candidates, 'Native suite did not provide the PDFium required by lazy_loading_test.dart')
    hashes = {upstream.digest(p) for p in candidates}
    upstream.require(len(hashes) == 1 and all(p.stat().st_size > 0 for p in candidates),
                     'Native suite produced ambiguous or empty PDFium modules')
    path = candidates[0].resolve()
    return path, {'path': str(path.relative_to(source.resolve())),
                  'sha256': upstream.digest(path), 'bytes': path.stat().st_size,
                  'reason': 'Run all six upstream lazy-loading/network tests gated by PDFIUM_PATH.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flutter', type=Path, required=True)
    parser.add_argument('--chrome', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--canary', action='store_true')
    parser.add_argument('--native-only', action='store_true',
                        help='Run every engine and VM viewer test; browser and Node remain in Linux lanes.')
    args = parser.parse_args()
    upstream.require(os.environ.get('CI') == 'true' and
                     os.environ.get('GITHUB_REPOSITORY') == 'yom-chadash/otzaria',
                     'These suites belong only to the authorized measurement fork')
    upstream.require(args.native_only or args.chrome is not None, 'Chrome is required for the full suite')
    upstream.require(not (args.native_only and args.canary), 'Desktop native lanes use the fixed SDK only')
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    upstream.require(not output.exists() and not output.is_relative_to(root),
                     'A new output directory outside the source is required')
    output.mkdir(parents=True)
    source = output / 'upstream'
    receipts = {}
    try:
        sdk = json.loads(upstream.run([args.flutter, '--version', '--machine'], root, output / 'flutter.json'))
        if not args.canary:
            upstream.require(sdk.get('frameworkVersion') == '3.50.0' and
                             sdk.get('frameworkRevision') == upstream.FLUTTER_SHA and
                             sdk.get('engineRevision') == upstream.ENGINE_SHA,
                             'The fixed baseline SDK changed')
        fork_sha = upstream.run(['git', 'rev-parse', 'HEAD'], root, output / 'fork-sha.log').strip()
        upstream.require(not upstream.run(['git', 'status', '--porcelain', '--untracked-files=all'],
                                         root, output / 'fork-status.log'), 'Fork source is dirty')
        source.mkdir()
        upstream.run(['git', 'init'], source, output / 'git-init.log')
        # The immutable-source receipt compares file bytes with git-show on every OS.
        upstream.run(['git', 'config', '--local', 'core.autocrlf', 'false'],
                     source, output / 'git-line-endings.log')
        upstream.run(['git', 'remote', 'add', 'origin', 'https://github.com/espresso3389/pdfrx.git'],
                     source, output / 'git-remote.log')
        upstream.run(['git', 'fetch', '--depth=1', 'origin', upstream.UPSTREAM_SHA], source, output / 'git-fetch.log')
        upstream.run(['git', 'checkout', '--detach', 'FETCH_HEAD'], source, output / 'git-checkout.log')
        upstream.require(upstream.run(['git', 'rev-parse', 'HEAD'], source, output / 'upstream-sha.log').strip()
                         == upstream.UPSTREAM_SHA, 'Upstream source changed')
        upstream.run([args.flutter, 'pub', 'get'], source, output / 'pub-get.log')
        upstream.run([args.flutter, 'pub', 'get', '--enforce-lockfile'], source, output / 'pub-get-enforced.log')
        frozen = upstream.freeze_resolved_source(source, output)
        shutil.copyfile(source / 'pubspec.lock', output / 'upstream-pubspec.lock')
        dart = args.flutter.resolve().parent / 'cache/dart-sdk/bin' / ('dart.exe' if os.name == 'nt' else 'dart')
        engine = source / 'packages/pdfrx_engine'
        viewer = source / 'packages/pdfrx'
        engine_tests = sorted((engine / 'test').rglob('*_test.dart'))
        browser_tests = sorted(p for p in (viewer / 'test').rglob('*_test.dart')
                               if "@TestOn('browser')" in p.read_text(encoding='utf-8'))
        viewer_tests = sorted(set((viewer / 'test').rglob('*_test.dart')) - set(browser_tests))
        node_tests = sorted((viewer / 'test').glob('*_test.cjs'))
        upstream.require(len(engine_tests) >= 25 and len(viewer_tests) >= 35 and
                         len(browser_tests) >= 8 and len(node_tests) == 10, 'Upstream suite inventory changed')
        groups = {
            'native': (engine, [dart, 'test', '--concurrency=1', '--reporter=json', *engine_tests]),
            'viewer': (viewer, [args.flutter, 'test', '--no-pub', '--concurrency=1', '--reporter=json', *viewer_tests]),
        }
        omitted = {}
        if args.native_only:
            omitted = {name: {'status': 'not-run-in-this-lane',
                              'reason': 'Real Chrome and Node suites run in the separate Linux lanes.'}
                       for name in ('chrome', 'node')}
        else:
            groups.update({
                'chrome': (viewer, [args.flutter, 'test', '--no-pub', '--platform', 'chrome', '--concurrency=1',
                                    '--reporter=json', *browser_tests]),
                'node': (viewer, ['node', '--test', '--test-reporter=tap', *node_tests]),
            })
        upstream.write_json(output / 'provenance.json', {
            'forkCommit': fork_sha, 'upstreamCommit': upstream.UPSTREAM_SHA, 'flutter': sdk,
            'canary': args.canary, 'appSdkChanged': False, 'performanceComparison': False,
            'platform': platform.system(), 'architecture': platform.machine(),
            'nativeOnly': args.native_only, 'omittedSuites': omitted,
            'resolvedSource': frozen,
            'tests': {name: [str(p.relative_to(source)) for p in paths]
                      for name, paths in [('native', engine_tests), ('viewer', viewer_tests),
                                          ('chrome', browser_tests), ('node', node_tests)]},
            'browserSelection': "@TestOn('browser') suites run in real Chrome, not skipped on VM",
        })
        failures = {}
        for name, (directory, command) in groups.items():
            log = output / (name + ('.tap' if name == 'node' else '.jsonl'))
            try:
                environment = {'CHROME_EXECUTABLE': str(args.chrome)} if args.chrome else {}
                if name == 'viewer':
                    pdfium, identity = viewer_pdfium(source)
                    upstream.write_json(output / 'viewer-pdfium.json', identity)
                    environment['PDFIUM_PATH'] = str(pdfium)
                text = upstream.run(command, directory, log, timeout=1800,
                                    extra_env=environment)
                if name == 'viewer':
                    upstream.require(upstream.digest(pdfium) == identity['sha256'],
                                     'Viewer PDFium module changed during the suite')
                receipts[name] = upstream.node_test_receipt(text) if name == 'node' else upstream.json_test_receipt(text)
                receipts[name]['status'] = 'passed'
            except Exception as error:
                failures[name] = str(error)
                receipts[name] = {'status': 'failed', 'error': str(error)}
            if name != 'node' and log.exists():
                receipts[name].update(dart_execution(log.read_text(encoding='utf-8')))
            # A failing suite must not hide other suites, but source drift stops everything.
            upstream.verify_frozen_source(source, output, frozen, name)
            upstream.write_json(output / 'suite-receipts.json', receipts)
        native_assets = {str(p.relative_to(source)): {'sha256': upstream.digest(p), 'bytes': p.stat().st_size}
                         for p in (source / '.dart_tool').rglob('*') if p.is_file()
                         and p.name in ('libpdfium.so', 'libpdfium.dylib', 'pdfium.dll', 'libpdfium_dart.dylib')}
        upstream.write_json(output / 'native-assets.json', native_assets)
        upstream.require(native_assets, 'Missing identity for the tested native PDFium')
        upstream.require(not failures, 'Failed upstream suites: ' + ', '.join(failures))
        upstream.write_json(output / 'summary.json', {'status': 'passed', 'canary': args.canary,
                                                     'upstreamCommit': upstream.UPSTREAM_SHA, 'suites': receipts,
                                                     'platform': platform.system(), 'nativeOnly': args.native_only,
                                                     'omittedSuites': omitted,
                                                     'appAdoptionProven': False})
    except BaseException:
        upstream.write_json(output / 'failure.json', {'status': 'failed', 'completedSuites': receipts,
                                                     'error': traceback.format_exc()})
        raise
    finally:
        if source.exists():
            shutil.rmtree(source)


if __name__ == '__main__':
    main()

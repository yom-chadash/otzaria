"""Run the upstream native, viewer and browser suites outside the application SDK."""

import argparse
import json
import os
from pathlib import Path
import shutil
import traceback

import internal_upstream_run as upstream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flutter', type=Path, required=True)
    parser.add_argument('--chrome', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--canary', action='store_true')
    args = parser.parse_args()
    upstream.require(os.environ.get('CI') == 'true' and
                     os.environ.get('GITHUB_REPOSITORY') == 'yom-chadash/otzaria',
                     'These suites belong only to the authorized measurement fork')
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
        dart = args.flutter.resolve().parent / 'cache/dart-sdk/bin/dart'
        engine = source / 'packages/pdfrx_engine'
        viewer = source / 'packages/pdfrx'
        engine_tests = sorted((engine / 'test').rglob('*_test.dart'))
        browser_tests = sorted(p for p in (viewer / 'test').rglob('*_test.dart')
                               if "@TestOn('browser')" in p.read_text())
        viewer_tests = sorted(set((viewer / 'test').rglob('*_test.dart')) - set(browser_tests))
        node_tests = sorted((viewer / 'test').glob('*_test.cjs'))
        upstream.require(len(engine_tests) >= 25 and len(viewer_tests) >= 35 and
                         len(browser_tests) >= 8 and len(node_tests) == 10, 'Upstream suite inventory changed')
        groups = {
            'native': (engine, [dart, 'test', '--concurrency=1', '--reporter=json', *engine_tests]),
            'viewer': (viewer, [args.flutter, 'test', '--no-pub', '--concurrency=1', '--reporter=json', *viewer_tests]),
            'chrome': (viewer, [args.flutter, 'test', '--no-pub', '--platform', 'chrome', '--concurrency=1',
                                '--reporter=json', *browser_tests]),
            'node': (viewer, ['node', '--test', '--test-reporter=tap', *node_tests]),
        }
        upstream.write_json(output / 'provenance.json', {
            'forkCommit': fork_sha, 'upstreamCommit': upstream.UPSTREAM_SHA, 'flutter': sdk,
            'canary': args.canary, 'appSdkChanged': False, 'performanceComparison': False,
            'resolvedSource': frozen,
            'tests': {name: [str(p.relative_to(source)) for p in paths]
                      for name, paths in [('native', engine_tests), ('viewer', viewer_tests),
                                          ('chrome', browser_tests), ('node', node_tests)]},
            'browserSelection': "@TestOn('browser') suites run in real Chrome, not skipped on VM",
        })
        failures = {}
        for name, (directory, command) in groups.items():
            try:
                text = upstream.run(command, directory, output / (name + ('.tap' if name == 'node' else '.jsonl')),
                                    timeout=1800, extra_env={'CHROME_EXECUTABLE': str(args.chrome)})
                receipts[name] = upstream.node_test_receipt(text) if name == 'node' else upstream.json_test_receipt(text)
            except Exception as error:
                failures[name] = str(error)
                receipts[name] = {'status': 'failed', 'error': str(error)}
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

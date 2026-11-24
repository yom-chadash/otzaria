"""Fork-only Linux profile experiment of the locked application's wrap search."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import urlopen

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tool/perf_probe'))
from tool.perf_probe import build_receipt as identity
from tool.perf_probe import run_perf_probe_posix as owned
from tool.perf_probe.posix_engine import BundledFlutterEngine
from tool.perf_probe.run_pdf_highlight_pair import load_documents
from tool.issue5538_ci.wrap_search_validate import validate_report, summarize

TARGET = 'tool/issue5538_ci/wrap_search_app.dart'
METRICS = 'tool/issue5538_ci/wrap_search_metrics.dart'
SEARCHER = 'lib/src/widgets/pdf_text_searcher.dart'
VIEWER_ARCHIVE_SHA = '69e2fdfae25c9686b336d89c3841766a1043bc5ba7f24bb8252224b837018440'
PDFIUM_SHA = '1c251efce460d67437a4c76d4be9033ae2f17bf5603f5bff1af10209b94ca723'
ORDERS = ('page1', 'start75', 'redirect')
BLOCKS = (ORDERS, tuple(reversed(ORDERS)), ('start75', 'page1', 'redirect'))
HARNESS = (TARGET, METRICS, 'tool/issue5538_ci/wrap_search_run.py',
           'tool/issue5538_ci/wrap_search_validate.py', 'tool/perf_probe/build_receipt.py',
           'tool/perf_probe/run_perf_probe_posix.py', 'tool/perf_probe/posix_engine.py',
           'tool/perf_probe/run_pdf_highlight_pair.py')
APP = ('lib/pdf_book/utils/centered_text_search_session.dart',
       'lib/pdf_book/view/pdf_search_screen.dart', 'lib/pdf_book/utils/pdf_user_regex_pattern.dart',
       'lib/search/utils/literal_search_pattern.dart')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def run(command, cwd, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    print(f'Running {log.stem}', flush=True)
    with log.open('x', encoding='utf-8') as output:
        completed = subprocess.run([str(x) for x in command], cwd=cwd,
                                   stdout=output, stderr=subprocess.STDOUT, check=False)
    require(completed.returncode == 0,
            f'{log.stem} failed ({completed.returncode}):\n{log.read_text(errors="replace")[-8000:]}')


def replace_once(text, old, new):
    require(text.count(old) == 1, f'Locked source anchor changed: {old[:90]}')
    return text.replace(old, new, 1)


def instrument(text, metrics_uri):
    text = replace_once(text, "import 'dart:collection';", "import 'dart:collection';\nimport '" + metrics_uri + "';")
    text = replace_once(text, '        final pageIndex = page.pageNumber - 1;', '''        final issue5538Started = Issue5538WrapMetrics.begin();
        final issue5538BeforeLength = textMatches.length;
        final issue5538IndexBefore = _currentIndex;
        final pageIndex = page.pageNumber - 1;''')
    text = replace_once(text, '        _matches = _MatchSnapshot(textMatches);', '''        _matches = _MatchSnapshot(textMatches);
        Issue5538WrapMetrics.finish(
          started: issue5538Started,
          page: page.pageNumber,
          beforeLength: issue5538BeforeLength,
          added: pageMatches.length,
          insertAt: insertAt,
          afterLength: textMatches.length,
          indexBefore: issue5538IndexBefore,
          indexAfter: _currentIndex,
        );''')
    return text


def file_manifest(root):
    return {path.relative_to(root).as_posix(): identity.file_record(path)
            for path in sorted(root.rglob('*')) if path.is_file()}


def extract_regular_archive(archive, destination):
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        require(not path.is_absolute() and '..' not in path.parts
                and '\\' not in member.name and (member.isdir() or member.isfile()),
                'Viewer archive contains an unsafe path or non-regular entry')
    destination.mkdir()
    for member in members:
        target = destination.joinpath(*PurePosixPath(member.name).parts)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
            require(target.stat().st_size == member.size, 'Incomplete viewer archive member')


def archive_viewer(output):
    archive = output / 'pdfrx-2.13.1.tar.gz'
    with urlopen('https://pub.dev/api/archives/pdfrx-2.13.1.tar.gz', timeout=120) as response, archive.open('xb') as target:
        shutil.copyfileobj(response, target)
    require(digest(archive) == VIEWER_ARCHIVE_SHA, 'Viewer archive differs from the locked package')
    original = output / 'viewer-original'
    with tarfile.open(archive, 'r:gz') as source:
        extract_regular_archive(source, original)
    require((original / SEARCHER).is_file(), 'Missing locked search implementation')
    write(output / 'viewer-archive.json', {'version': '2.13.1', 'sha256': digest(archive),
          'bytes': archive.stat().st_size, 'sourceUrl': 'https://pub.dev/api/archives/pdfrx-2.13.1.tar.gz',
          'searcherSha256': digest(original / SEARCHER)})
    return original


def prepare_overlay(checkout, variant, original, output):
    config_path = checkout / '.dart_tool/package_config.json'
    config = json.loads(config_path.read_text())
    for package in config['packages']:
        package['rootUri'] = urljoin(config_path.as_uri(), package['rootUri'])
    viewer = next(p for p in config['packages'] if p['name'] == 'pdfrx')
    resolved_uri = urlparse(viewer['rootUri'])
    require(resolved_uri.scheme == 'file', 'Resolved viewer is not local')
    resolved = Path(unquote(resolved_uri.path))
    for path in sorted((original / 'lib').rglob('*')):
        if path.is_file():
            relative = path.relative_to(original)
            require(digest(resolved / relative) == digest(path), f'Resolved viewer differs from archive: {relative}')
    copied = output / 'viewer'
    shutil.copytree(original, copied)
    before = (copied / SEARCHER).read_text()
    if variant == 'counts':
        after = instrument(before, (checkout / METRICS).as_uri())
        (copied / SEARCHER).write_text(after)
        (output / 'instrumentation.patch').write_text(''.join(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True), fromfile=SEARCHER, tofile=SEARCHER)), encoding='utf-8')
    else:
        after = before
    viewer['rootUri'] = copied.as_uri() + '/'
    overlay = output / 'packages.json'
    write(overlay, config)
    write(output / 'overlay-receipt.json', {'variant': variant, 'officialConfig': identity.file_record(config_path),
          'officialGraph': identity.file_record(checkout / '.dart_tool/package_graph.json'),
          'overlayConfig': identity.file_record(overlay), 'originalArchiveSha256': VIEWER_ARCHIVE_SHA,
          'beforeSearcherSha256': hashlib.sha256(before.encode()).hexdigest(),
          'afterSearcherSha256': hashlib.sha256(after.encode()).hexdigest(),
          'changedPackageFiles': [SEARCHER] if variant == 'counts' else [],
          'patchPurpose': 'observation only; result buffering and all navigation decisions remain unchanged'})
    return copied, overlay


def compiler_inputs(checkout, viewer):
    candidates = []
    required = {(checkout / TARGET).resolve(), (checkout / METRICS).resolve(),
                (checkout / APP[0]).resolve(), (viewer / SEARCHER).resolve()}
    for depfile in (checkout / '.dart_tool/flutter_build').glob('*/kernel_snapshot_program.d'):
        sides = depfile.read_text().split(': ')
        require(len(sides) == 2, 'Invalid compiler depfile')
        paths = {Path(x) if Path(x).is_absolute() else checkout / x for x in identity.depfile_paths(sides[1])}
        if required <= {p.resolve() for p in paths}:
            candidates.append((depfile, paths))
    require(len(candidates) == 1, 'Need one compiler depfile proving the actual app/session/overlay entrypoints')
    depfile, paths = candidates[0]
    return {'depfile': identity.file_record(depfile),
            'inputs': [identity.file_record(p) for p in sorted(paths, key=str)]}


def snapshot(checkout, viewer, overlay, executable):
    sdk = identity.sdk_identity(checkout)
    sdk_engine = Path(sdk['root']) / 'bin/cache/artifacts/engine/linux-x64-profile/libflutter_linux_gtk.so'
    require(digest(sdk_engine) == digest(executable.parent / 'lib/libflutter_linux_gtk.so'),
            'Bundled Flutter engine differs from the pinned SDK profile engine')
    return {'source': identity.source_identity(checkout), 'sdk': sdk,
            'sdkProfileEngine': identity.file_record(sdk_engine),
            'compilerTools': identity.compiler_tools(sdk),
            'appAndHarness': {p: identity.file_record(checkout / p) for p in (*HARNESS, *APP)},
            'packageConfig': identity.file_record(checkout / '.dart_tool/package_config.json'),
            'packageGraph': identity.file_record(checkout / '.dart_tool/package_graph.json'),
            'overlay': identity.file_record(overlay), 'viewerFiles': file_manifest(viewer),
            'compiler': compiler_inputs(checkout, viewer),
            'bundle': identity.bundle_identity(executable, 'linux')}


def assert_snapshot(info):
    current = snapshot(info['checkout'], info['viewer'], info['overlay'], info['executable'])
    require(current == info['snapshot'], 'Source, dependency, compiler, SDK or full bundle changed; refusing reuse')
    return current


def wait_file(process, path, timeout):
    deadline = time.monotonic() + timeout
    while not path.is_file():
        code = owned.child_exit(process)
        require(code is None, f'Owned app exited before {path.name}: {code}')
        require(time.monotonic() < deadline, f'Timeout before {path.name}')
        time.sleep(.05)
    return json.loads(path.read_text())


def mapped_library(pid, path):
    text = Path(f'/proc/{pid}/maps').read_text()
    lines = [line for line in text.splitlines() if line.split(maxsplit=5)[-1] == str(path.resolve())]
    require(lines, f'Expected library is not mapped by owned process: {path}')
    actual = path.stat()
    for line in lines:
        fields = line.split(maxsplit=5)
        require(len(fields) == 6 and int(fields[4]) == actual.st_ino,
                'Mapped library inode differs')
        major, minor = (int(value, 16) for value in fields[3].split(':'))
        require((major, minor) == (os.major(actual.st_dev), os.minor(actual.st_dev)), 'Mapped library device differs')
    return {'path': str(path.resolve()), 'identity': identity.file_record(path), 'mappingLines': lines}


def launch(info, document, pdfium, order, output):
    output.mkdir(parents=True, exist_ok=False)
    native = output / 'native'
    assert_snapshot(info)  # Completed before the measured process starts.
    require(digest(pdfium) == PDFIUM_SHA and digest(document['absolutePath']) == document['sha256'], 'Input changed')
    environment = {**os.environ, 'WRAP_SEARCH_OUTPUT': str(native),
        'WRAP_SEARCH_INPUT': document['absolutePath'], 'WRAP_SEARCH_SHA256': document['sha256'],
        'WRAP_SEARCH_PAGES': str(document['pageCount']), 'WRAP_SEARCH_PDFIUM': str(pdfium),
        'WRAP_SEARCH_ORDER': order}
    write(output / 'launch.json', {'command': [str(info['executable'])],
          'environment': {k: v for k, v in environment.items() if k.startswith('WRAP_SEARCH_')}})
    engine = BundledFlutterEngine(info['executable'], 'linux', 'profile')
    with (output / 'process.log').open('x', encoding='utf-8') as log:
        process = subprocess.Popen([str(info['executable'])], cwd=info['executable'].parent,
                                   env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            ready = wait_file(process, native / 'ready.json', 600)
            require(ready['processId'] == process.pid and ready['sourceCommit'] == info['commit']
                    and ready['variant'] == info['variant'] and ready['measurementsStarted'] is False,
                    'Native preparation identity mismatch')
            mapping = {'flutter': engine.observe_loaded(process.pid),
                       'pdfium': mapped_library(process.pid, pdfium),
                       'searchEngine': mapped_library(process.pid, info['executable'].parent / 'lib/libsearch_engine.so')}
            write(output / 'native-mappings-before.json', mapping)
            require(mapping['searchEngine']['identity']['sha256'] == ready['searchEngineSha256'], 'Search engine identity mismatch')
            (native / 'go').touch(exist_ok=False)  # No hashes while app timings run.
            completion = wait_file(process, native / 'completion.json', 1800)
            require(completion['measurementsFinished'] is True and completion['processId'] == process.pid
                    and completion['sourceCommit'] == info['commit'] and completion['status'] == 'passed',
                    'Missing successful measurement completion')
            require(digest(native / 'report.json') == completion['reportSha256'], 'Completion/report hash mismatch')
            report = validate_report(json.loads((native / 'report.json').read_text()))
            require(report['inputSha256'] == document['sha256'] and report['pageCount'] == document['pageCount']
                    and report['sourceCommit'] == info['commit'] and report['variant'] == info['variant']
                    and report['scanCondition'] == order, 'Report identity mismatch')
            require(digest(native / 'reference.json') == report['referenceSha256'], 'Reference hash mismatch')
            write(output / 'native-mappings-after.json', {'flutter': engine.observe_loaded(process.pid),
                  'pdfium': mapped_library(process.pid, pdfium),
                  'searchEngine': mapped_library(process.pid, info['executable'].parent / 'lib/libsearch_engine.so')})
            (native / 'stop').touch(exist_ok=False)
            deadline = time.monotonic() + 30
            while owned.child_exit(process) is None:
                require(time.monotonic() < deadline, 'App did not exit after successful handshake')
                time.sleep(.05)
            require(owned.child_exit(process) == 0, 'App exited unsuccessfully after completion')
        finally:
            # child_exit uses waitid(WNOWAIT); group remains owned until cleanup.
            owned.cleanup_group(process)
    assert_snapshot(info)  # Cleanup and all app work precede the after hash.
    require(digest(pdfium) == PDFIUM_SHA and digest(document['absolutePath']) == document['sha256'], 'Input changed')
    write(output / 'validated.json', {'status': 'passed', 'reportSha256': digest(native / 'report.json'),
          'buildSnapshotSha256': info['snapshotSha256'], 'ownedPid': process.pid})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', required=True, type=Path)
    parser.add_argument('--pdfium', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    require(platform.system() == 'Linux' and platform.machine() == 'x86_64', 'Requires Linux x64')
    require(os.getenv('DISPLAY'), 'Launch inside the configured Xvfb desktop')
    output = args.output.resolve()
    require(not output.exists() and not output.is_relative_to(ROOT), 'Output must be new and outside the source')
    output.mkdir(parents=True)
    results, checkouts = [], []
    try:
        run(['flutter', '--version'], ROOT, output / 'sdk-bootstrap.log')
        source = identity.source_identity(ROOT)
        commit = source['commit']
        sdk = identity.sdk_identity(ROOT)
        write(output / 'source.json', source)
        write(output / 'sdk.json', sdk)
        documents = load_documents(args.corpus.resolve(), ['uxlc-genesis', 'mikraot-gedolot-genesis-1859'])
        write(output / 'corpus.json', {'manifestSha256': digest(args.corpus), 'documents': documents})
        pdfium = args.pdfium.resolve(strict=True)
        require(digest(pdfium) == PDFIUM_SHA, 'Unexpected PDFium')
        original = archive_viewer(output)
        builds = {}
        for variant in ('control', 'counts'):
            directory = output / 'builds' / variant
            directory.mkdir(parents=True)
            checkout = output / f'source-{variant}'
            run(['git', 'worktree', 'add', '--detach', checkout, commit], ROOT, directory / 'checkout.log')
            checkouts.append(checkout)
            run(['flutter', 'pub', 'get', '--enforce-lockfile'], checkout, directory / 'pub-get.log')
            require(identity.source_identity(checkout)['commit'] == commit, 'Source revision changed')
            lock = (checkout / 'pubspec.lock').read_text()
            require(VIEWER_ARCHIVE_SHA in lock and '001090d220b2773747ce396f2bcda3d6ac9389d87f937ef12d64ed3a9e80e8b0' in lock,
                    'Locked PDF packages changed')
            viewer, overlay = prepare_overlay(checkout, variant, original, directory)
            run(['flutter', 'analyze', '--no-pub', TARGET, METRICS], checkout, directory / 'analyze.log')
            command = ['flutter', f'--packages={overlay}', 'build', 'linux', '--profile', '--no-pub',
                       f'--target={TARGET}', f'--dart-define=WRAP_SEARCH_SOURCE={commit}',
                       f'--dart-define=WRAP_SEARCH_INSTRUMENTED={str(variant == "counts").lower()}']
            write(directory / 'build-command.json', command)
            run(command, checkout, directory / 'build.log')
            executable = (checkout / 'build/linux/x64/profile/bundle/otzaria').resolve(strict=True)
            frozen = snapshot(checkout, viewer, overlay, executable)
            write(directory / 'build-snapshot.json', frozen)
            builds[variant] = {'checkout': checkout, 'viewer': viewer, 'overlay': overlay,
                               'executable': executable, 'snapshot': frozen, 'commit': commit, 'variant': variant,
                               'snapshotSha256': digest(directory / 'build-snapshot.json')}
        for document in documents:
            for block, orders in enumerate(BLOCKS):
                for order in orders:
                    destination = output / 'runs' / document['id'] / f'control-b{block}-{order}'
                    report = launch(builds['control'], document, pdfium, order, destination)
                    results.append({'block': block, 'path': str(destination.relative_to(output)), 'report': report})
                    write(output / 'partial-summary.json', summarize(results, complete=False))
            for order in ORDERS:
                destination = output / 'runs' / document['id'] / f'counts-{order}'
                report = launch(builds['counts'], document, pdfium, order, destination)
                results.append({'block': None, 'path': str(destination.relative_to(output)), 'report': report})
                write(output / 'partial-summary.json', summarize(results, complete=False))
        final = summarize(results, complete=True)
        write(output / 'summary.json', final)
        print(json.dumps({'status': 'passed', 'independentProcesses': len(results), 'builds': 2}))
        return 0
    except Exception as error:
        write(output / 'failure.json', {'status': 'failed', 'error': str(error), 'stack': traceback.format_exc(),
              'completedIndependentProcesses': len(results)})
        raise
    finally:
        for checkout in reversed(checkouts):
            # Git owns these detached worktrees; never delete another process's directory.
            subprocess.run(['git', 'worktree', 'remove', '--force', str(checkout)], cwd=ROOT, check=False)


if __name__ == '__main__':
    main()

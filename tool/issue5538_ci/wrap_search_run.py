"""Fork-only Linux profile experiment of the locked application's wrap search."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
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
from tool.issue5538_ci.wrap_search_validate import validate_queries, validate_report, summarize

TARGET = 'tool/issue5538_ci/wrap_search_app.dart'
METRICS = 'tool/issue5538_ci/wrap_search_metrics.dart'
QUERIES = 'tool/issue5538_ci/wrap_search_queries.dart'
QUERY_TEST = 'tool/issue5538_ci/wrap_search_queries_test.dart'
SEARCHER = 'lib/src/widgets/pdf_text_searcher.dart'
VIEWER_ARCHIVE_SHA = '69e2fdfae25c9686b336d89c3841766a1043bc5ba7f24bb8252224b837018440'
PDFIUM_SHA = '1c251efce460d67437a4c76d4be9033ae2f17bf5603f5bff1af10209b94ca723'
ORDERS = ('page1', 'start75', 'redirect')
BLOCKS = (ORDERS, tuple(reversed(ORDERS)), ('start75', 'page1', 'redirect'))
HARNESS = (TARGET, METRICS, QUERIES, QUERY_TEST, 'test/support/search_engine_test_init.dart',
           'tool/issue5538_ci/wrap_search_run.py',
           'tool/issue5538_ci/wrap_search_validate.py', 'tool/perf_probe/build_receipt.py',
           'tool/perf_probe/run_perf_probe_posix.py', 'tool/perf_probe/posix_engine.py',
           'tool/perf_probe/run_pdf_highlight_pair.py')
APP = ('lib/pdf_book/utils/centered_text_search_session.dart',
       'lib/pdf_book/view/pdf_search_screen.dart', 'lib/pdf_book/utils/pdf_user_regex_pattern.dart',
       'lib/search/utils/literal_search_pattern.dart', 'lib/search/utils/user_regex_query.dart')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def run(command, cwd, log, environment=None):
    log.parent.mkdir(parents=True, exist_ok=True)
    print(f'Running {log.stem}', flush=True)
    with log.open('x', encoding='utf-8') as output:
        completed = subprocess.run([str(x) for x in command], cwd=cwd,
                                   stdout=output, stderr=subprocess.STDOUT, check=False,
                                   env=environment)
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
    installed = install_project_overlay(checkout, copied, overlay)
    write(output / 'overlay-receipt.json', {'variant': variant, 'officialConfig': installed['officialConfigBackup'],
          'officialGraph': installed['officialGraphBackup'],
          'resolution': installed,
          'overlayConfig': identity.file_record(overlay), 'originalArchiveSha256': VIEWER_ARCHIVE_SHA,
          'beforeSearcherSha256': hashlib.sha256(before.encode()).hexdigest(),
          'afterSearcherSha256': hashlib.sha256(after.encode()).hexdigest(),
          'changedPackageFiles': [SEARCHER] if variant == 'counts' else [],
          'patchPurpose': 'observation only; result buffering and all navigation decisions remain unchanged'})
    return copied, overlay


def normalized_config(config, path):
    result = json.loads(json.dumps(config))
    for package in result['packages']:
        package['rootUri'] = urljoin(path.as_uri(), package['rootUri'])
    return result


def install_project_overlay(checkout, viewer, overlay):
    """Use the generated config read by this SDK's Linux KernelSnapshot.

    Only the owned throwaway worktree's generated files are involved. The
    official pub output remains as exact backup bytes, and the declared private
    viewer is the only package-root delta. No resolver or Pub cache is changed.
    """
    config = checkout / '.dart_tool/package_config.json'
    graph = config.with_name('package_graph.json')
    require(config.is_file() and graph.is_file() and not config.is_symlink()
            and not graph.is_symlink(), 'Missing or aliased official pub resolution')
    before_config, before_graph = identity.file_record(config), identity.file_record(graph)
    official = normalized_config(json.loads(config.read_text()), config)
    expected = json.loads(json.dumps(official))
    matches = [package for package in expected['packages'] if package['name'] == 'pdfrx']
    require(len(matches) == 1, 'Official resolution must contain one viewer package')
    matches[0]['rootUri'] = viewer.as_uri() + '/'
    require(json.loads(overlay.read_text()) == expected,
            'Private mapping changed more than the declared viewer root')
    saved_config = overlay.parent / 'official-package-config.json'
    saved_graph = overlay.parent / 'official-package-graph.json'
    require(not saved_config.exists() and not saved_graph.exists(), 'Official resolution backup must be new')
    shutil.copyfile(config, saved_config)
    shutil.copyfile(graph, saved_graph)
    require(digest(saved_config) == before_config['sha256'] == digest(config)
            and digest(saved_graph) == before_graph['sha256'] == digest(graph),
            'Official resolution changed while preserving it')
    shutil.copyfile(overlay, config)
    require(config.read_bytes() == overlay.read_bytes() and graph.read_bytes() == saved_graph.read_bytes(),
            'Installed viewer mapping or unchanged package graph differs')
    receipt = {
        'method': 'Declared generated package_config overlay in an owned temporary worktree; Linux KernelSnapshot reads this path, not the outer --packages flag.',
        'resolver': 'flutter pub get --enforce-lockfile completed before the overlay; no resolver rerun',
        'changedPackageRoots': ['pdfrx'],
        'officialProjectConfigBeforeOverlay': before_config,
        'officialProjectGraph': before_graph,
        'officialConfigBackup': identity.file_record(saved_config),
        'officialGraphBackup': identity.file_record(saved_graph),
        'privateMapping': identity.file_record(overlay),
        'installedProjectConfig': identity.file_record(config),
        'installedProjectGraph': identity.file_record(graph),
    }
    write(overlay.parent / 'installed-resolution.json', receipt)
    return receipt


def verify_installed_overlay(checkout, overlay):
    path = overlay.parent / 'installed-resolution.json'
    receipt = json.loads(path.read_text())
    expected_paths = {
        'officialConfigBackup': overlay.parent / 'official-package-config.json',
        'officialGraphBackup': overlay.parent / 'official-package-graph.json',
        'privateMapping': overlay,
        'installedProjectConfig': checkout / '.dart_tool/package_config.json',
        'installedProjectGraph': checkout / '.dart_tool/package_graph.json',
    }
    for name, expected in expected_paths.items():
        require(expected.is_file() and not expected.is_symlink()
                and identity.file_record(expected) == receipt[name],
                f'Official/private resolution changed: {name}')
    require(receipt['installedProjectConfig']['sha256'] == receipt['privateMapping']['sha256']
            and receipt['installedProjectGraph']['sha256'] == receipt['officialProjectGraph']['sha256']
            == receipt['officialGraphBackup']['sha256']
            and receipt['officialConfigBackup']['sha256'] == receipt['officialProjectConfigBeforeOverlay']['sha256'],
            'Official/private resolution receipt is inconsistent')
    return {'receipt': identity.file_record(path), 'resolution': receipt}


def preserve_compiler_records(checkout, directory):
    """Keep exact small build-system records even if the build/guard fails.

    .log/.json names match the existing artifact glob; no build or corpus binary
    is copied. These are compiler records, not inferred application observations.
    """
    build = checkout / '.dart_tool/flutter_build'
    paths = sorted({*build.glob('*/*.d'), *build.glob('*/kernel_snapshot_program.stamp')})
    records = []
    for index, path in enumerate(paths):
        require(path.is_file() and not path.is_symlink(), 'Compiler record is not a regular file')
        copied = directory / f'compiler-record-{index:03d}.log'
        require(not copied.exists(), 'Compiler evidence must be new')
        shutil.copyfile(path, copied)
        require(digest(path) == digest(copied), 'Compiler record changed while preserving it')
        records.append({'original': identity.file_record(path), 'copy': identity.file_record(copied)})
    write(directory / 'compiler-records.json', {'scope': 'Exact raw Flutter build records, collected before source cleanup.', 'records': records})


def compiler_inputs(checkout, viewer, diagnostics=None):
    candidates = []
    required = {(checkout / TARGET).resolve(), (checkout / METRICS).resolve(), (checkout / QUERIES).resolve(),
                (checkout / APP[0]).resolve(), (viewer / SEARCHER).resolve()}
    findings = []
    for depfile in sorted((checkout / '.dart_tool/flutter_build').glob('*/kernel_snapshot_program.d')):
        row = {'depfile': identity.file_record(depfile)}
        findings.append(row)
        try:
            sides = depfile.read_text().split(': ')
            require(len(sides) == 2, 'Invalid compiler depfile')
            def paths_from(names):
                return {Path(x) if Path(x).is_absolute() else checkout / x for x in names}
            paths = paths_from(identity.depfile_paths(sides[1]))
            outputs = paths_from(identity.depfile_paths(sides[0]))
            canonical = {p.resolve() for p in paths}
            row['inputCount'] = len(paths)
            row['missingRequiredInputs'] = sorted(map(str, required - canonical))
            row['searcherInputs'] = sorted(str(p) for p in paths if p.name == Path(SEARCHER).name)
            kernel = depfile.parent / 'app.dill'
            row['outputs'] = sorted(map(str, outputs))
            require(kernel.resolve() in {p.resolve() for p in outputs} and kernel.is_file()
                    and kernel.stat().st_size > 0, 'Depfile does not produce the official nonempty app.dill')
            stamp = depfile.with_name('kernel_snapshot_program.stamp')
            require(stamp.is_file() and not stamp.is_symlink(), 'Missing official kernel build stamp')
            stamp_data = json.loads(stamp.read_text())
            require(isinstance(stamp_data.get('inputs'), list) and isinstance(stamp_data.get('outputs'), list),
                    'Malformed official kernel build stamp')
            stamp_inputs = {p.resolve() for p in paths_from(stamp_data['inputs'])}
            stamp_outputs = {p.resolve() for p in paths_from(stamp_data['outputs'])}
            config = (checkout / '.dart_tool/package_config.json').resolve()
            row['stampIncludesInstalledPackageConfig'] = config in stamp_inputs
            require(config in stamp_inputs and required <= stamp_inputs and kernel.resolve() in stamp_outputs,
                    'Official kernel stamp does not prove the installed package config and required inputs/output')
            require(not row['missingRequiredInputs'], 'Compiler used different app/session/observer/viewer inputs')
            # Hash every real compiler input, including the build-system package
            # mapping, rather than merely checking suffixes or source text names.
            inputs = paths | paths_from(stamp_data['inputs'])
            candidates.append({'depfile': identity.file_record(depfile), 'stamp': identity.file_record(stamp),
                'kernel': identity.file_record(kernel), 'installedPackageConfig': identity.file_record(config),
                'inputs': [identity.file_record(p) for p in sorted(inputs, key=str)]})
            row['accepted'] = True
        except (ValueError, OSError, TypeError, KeyError) as error:
            row['accepted'] = False
            row['error'] = str(error)
    if diagnostics is not None:
        write(diagnostics / 'compiler-input-diagnostics.json', {'required': sorted(map(str, required)),
              'candidates': findings, 'matchingDepfiles': len(candidates)})
    require(len(candidates) == 1, 'Need one compiler depfile proving the actual app/session/overlay entrypoints')
    return candidates[0]


def snapshot(checkout, viewer, overlay, executable, diagnostics=None):
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
            'declaredPackageOverlay': verify_installed_overlay(checkout, overlay),
            'overlay': identity.file_record(overlay), 'viewerFiles': file_manifest(viewer),
            'compiler': compiler_inputs(checkout, viewer, diagnostics),
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


def validate_preflight(report, documents, commit):
    require(report.get('schemaVersion') == 1 and report.get('status') == 'passed'
            and report.get('sourceCommit') == commit and type(report.get('processId')) is int
            and report['processId'] > 0 and report.get('pdfiumSha256') == PDFIUM_SHA,
            'Query preflight identity or completion differs')
    require(isinstance(report.get('searchEngineSha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', report['searchEngineSha256']),
            'Query preflight native search engine receipt is missing')
    rows = report['documents']
    require([row['id'] for row in rows] == [document['id'] for document in documents],
            'Query preflight omitted or reordered a native corpus document')
    for row, document in zip(rows, documents):
        require(row['inputSha256'] == document['sha256'] and row['pageCount'] == document['pageCount']
                and row['targetPage'] == document['pageCount'] * 3 // 4,
                'Query preflight corpus or wrap boundary differs')
        validate_queries(row['queries'])
        descriptor_sha = hashlib.sha256(json.dumps(row['queries'], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        require(row['queryDescriptorsSha256'] == descriptor_sha, 'Preflight query descriptor hash differs')
        require(isinstance(row['pageTextHashesSha256'], str)
                and re.fullmatch(r'[0-9a-f]{64}', row['pageTextHashesSha256']),
                'Preflight native text hash is missing')
    return {row['id']: row for row in rows}


def query_preflight(checkout, overlay, documents, pdfium, commit, output):
    """Run the shared query builder against native text before either AOT build."""
    path = output / 'query-preflight-config.json'
    report_path = output / 'query-preflight-report.json'
    write(path, {'sourceCommit': commit, 'pdfium': str(pdfium), 'pdfiumSha256': PDFIUM_SHA,
                 'documents': documents, 'output': str(report_path)})
    before = identity.source_identity(checkout)
    require(before['commit'] == commit, 'Preflight source revision differs')
    resolution = verify_installed_overlay(checkout, overlay)
    command = ['flutter', 'test', '--no-pub', '--concurrency=1', '--reporter=expanded', QUERY_TEST]
    write(output / 'query-preflight-command.json', command)
    run(command, checkout, output / 'query-preflight.log',
        environment={**os.environ, 'WRAP_SEARCH_QUERY_PREFLIGHT': str(path)})
    require(identity.source_identity(checkout) == before
            and verify_installed_overlay(checkout, overlay) == resolution,
            'Source or package resolution changed during the query gate')
    report = json.loads(report_path.read_text())
    result = validate_preflight(report, documents, commit)
    write(output / 'query-preflight-validated.json', {
        'status': 'passed', 'scope': 'Early debug/JIT functional gate, not a performance comparison',
        'report': identity.file_record(report_path), 'config': identity.file_record(path),
        'source': before, 'resolution': resolution,
        'sharedQuerySource': identity.file_record(checkout / QUERIES),
        'testSource': identity.file_record(checkout / QUERY_TEST),
        'runtimeComparison': 'Both profile variants must reproduce these descriptors and native text hashes; debug gate binary is not a timed baseline.'})
    return result


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
            preflight = info['queryPreflight'][document['id']]
            require(ready['queryDescriptorsSha256'] == preflight['queryDescriptorsSha256'],
                    'Profile query descriptors differ from the early product-parser gate')
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
            require(report['queries'] == preflight['queries']
                    and report['pageTextHashesSha256'] == preflight['pageTextHashesSha256'],
                    'Profile query selection or native extraction differs from preflight')
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
        preflight = None
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
            run(['flutter', 'analyze', '--no-pub', TARGET, METRICS, QUERIES, QUERY_TEST], checkout, directory / 'analyze.log')
            verify_installed_overlay(checkout, overlay)
            if preflight is None:
                preflight = query_preflight(checkout, overlay, documents, pdfium, commit, output)
            command = ['flutter', 'build', 'linux', '--profile', '--no-pub',
                       f'--target={TARGET}', f'--dart-define=WRAP_SEARCH_SOURCE={commit}',
                       f'--dart-define=WRAP_SEARCH_INSTRUMENTED={str(variant == "counts").lower()}']
            write(directory / 'build-command.json', command)
            try:
                run(command, checkout, directory / 'build.log')
            finally:
                preserve_compiler_records(checkout, directory)
            executable = (checkout / 'build/linux/x64/profile/bundle/otzaria').resolve(strict=True)
            frozen = snapshot(checkout, viewer, overlay, executable, directory)
            write(directory / 'build-snapshot.json', frozen)
            builds[variant] = {'checkout': checkout, 'viewer': viewer, 'overlay': overlay,
                               'executable': executable, 'snapshot': frozen, 'commit': commit, 'variant': variant,
                               'queryPreflight': preflight,
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

"""Replay pinned S02 raw artifacts with the full strict validator; no native run.

Only PdfReader's fixture file access is relocated to an immutable, hash-checked
copy. Original configs, event paths/owners, PNGs and verdict files are untouched.
The original Actions conclusions are retained. This is validation, not timing.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from PIL import Image
from pypdf import PdfReader

APP = ('lib/pdf_book/utils/pdf_font_fallback.dart',
       'lib/pdf_book/utils/pdf_document_font_recovery.dart',
       'lib/indexing/repository/indexing_repository.dart',
       'lib/library/view/book_preview_panel.dart')
RUNTIME = (*APP, 'test/pdf_book/pdf_document_font_recovery_test.dart',
           'test/pdf_book/pdf_font_fallback_lifecycle_test.dart', 'pubspec.yaml', 'pubspec.lock')
TARGET = 'tool/issue5538_ci/font_fallback_profile_app.dart'
HOST = 'tool/issue5538_ci/run_font_fallback_profile.py'
PDFIUM = '1c251efce460d67437a4c76d4be9033ae2f17bf5603f5bff1af10209b94ca723'
ENGINE = '964b5e15e082c8633651ffab1d037631af65e156'
ARCHIVES = {'pdfrx': '69e2fdfae25c9686b336d89c3841766a1043bc5ba7f24bb8252224b837018440',
            'pdfrx_engine': '001090d220b2773747ce396f2bcda3d6ac9389d87f937ef12d64ed3a9e80e8b0'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    require(not path.exists(), 'Derived output already exists: ' + str(path))
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def extract_verified(archive, spec, directory):
    require(archive.is_file() and not archive.is_symlink()
            and archive.stat().st_size == spec['zipSizeBytes']
            and digest(archive) == spec['zipSha256'], 'Pinned artifact ZIP hash/size differs')
    require(not directory.exists(), 'Extraction directory must be new')
    with zipfile.ZipFile(archive) as source:
        records, seen = [], set()
        for item in source.infolist():
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            require(not path.is_absolute() and '..' not in path.parts and '\\' not in item.filename
                    and item.filename not in seen and not stat.S_ISLNK(mode), 'Unsafe or duplicate artifact member')
            require(stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR), 'Non-regular artifact member')
            seen.add(item.filename)
            if not item.is_dir():
                require(item.file_size <= 32 * 1024 * 1024, 'Oversized artifact member')
                records.append(item)
        require(len(records) == spec['memberCount'] and sum(item.file_size for item in records) <= 256 * 1024 * 1024,
                'Artifact member coverage/size differs')
        directory.mkdir(parents=True)
        manifest = {}
        for item in records:
            content = source.read(item)
            target = directory / item.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            manifest[item.filename] = {'sha256': hashlib.sha256(content).hexdigest(), 'sizeBytes': len(content)}
    verify_members(directory, manifest)
    return manifest


def verify_members(directory, manifest):
    for relative, expected in manifest.items():
        path = directory / relative
        require(path.is_file() and not path.is_symlink() and path.stat().st_size == expected['sizeBytes']
                and digest(path) == expected['sha256'], 'Original artifact member drifted: ' + relative)


def fixture_mapping(raw, config, manifest, mapping):
    documents = next(row['documents'] for row in manifest['cases'] if row['id'] == config['scenario'])
    require([{k: v for k, v in row.items() if k != 'absolutePath'} for row in config['documents']] == documents,
            'Launch fixture manifest differs')
    for item in config['documents']:
        relative = PurePosixPath(item['path'])
        require(not relative.is_absolute() and '..' not in relative.parts and '\\' not in item['path'],
                'Fixture path escapes raw evidence')
        path = raw / 'fixtures' / relative
        require(path.is_file() and not path.is_symlink() and digest(path) == item['sha256']
                and path.stat().st_size == item['sizeBytes'], 'Relocated fixture hash/size differs')
        require(item['absolutePath'].endswith('/fixtures/' + item['path']), 'Original fixture source path differs')
        record = (path, item['sha256'], item['sizeBytes'])
        require(item['absolutePath'] not in mapping or mapping[item['absolutePath']] == record,
                'Ambiguous fixture relocation')
        mapping[item['absolutePath']] = record


def load_validation_functions(path, expected_sha, mapping):
    require(digest(path) == expected_sha, 'Full strict validator source SHA differs')
    names = {'require', 'json_hash', 'fixture_font_resources', 'font_query_key',
             'validate_pngs', 'validate_replacement_lifecycle', 'validate_result'}
    module = ast.parse(path.read_text())
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in names]
    require({node.name for node in functions} == names, 'Full strict validation function set is missing')

    def mapped_pdf_reader(original_path):
        require(original_path in mapping, 'Full validator requested an unverified original PDF path')
        local, checksum, size = mapping[original_path]
        require(digest(local) == checksum and local.stat().st_size == size, 'Fixture changed during full replay')
        return PdfReader(local)

    namespace = {'hashlib': hashlib, 'json': json, 'Path': Path, 'Image': Image,
                 'PdfReader': mapped_pdf_reader, 'digest': digest}
    # Function bodies are copied as complete AST nodes, without editing or
    # replacing any check. Imports/launch/build/main code are not executed.
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def verify_source_receipts(raw, report, spec):
    require(report['workflow']['GITHUB_RUN_ID'] == str(spec['runId'])
            and report['workflow']['GITHUB_SHA'] == spec['sourceCommit']
            and report['plan'] == spec['plan'] and report['status'] == spec['originalStatus'],
            'Original workflow/plan/conclusion differs')
    require(report['sdk']['frameworkRevision'] == 'abaf9c523780a608bd46686fd5e53740a07077f8'
            and report['sdk']['engineRevision'] == 'deb287481e3ce9468f3434937ced4240a70539ca'
            and report['sdk']['frameworkVersion'] == '3.47.7'
            and report['sdk']['dartSdkVersion'] == '3.13.5', 'SDK receipt differs')
    common = report['causalPlan']['commonHarness']
    require(common[HOST] == spec['originalValidatorSha256'], 'Original host receipt differs')
    require(all(report['orchestrator']['files'][path] == checksum for path, checksum in common.items()),
            'Original orchestrator harness differs')
    for name, source in report['sources'].items():
        require(source['commit'] == spec['plan'][name]
                and all(source['files'][path] == checksum for path, checksum in common.items()),
                'Measured source/harness receipt differs')
    require(all(report['sources']['A']['files'][path] == report['sources']['B']['files'][path] for path in APP),
            'A/B changes application code')
    require(all(report['sources']['B']['files'][path] == report['sources']['C']['files'][path]
                for path in ('pubspec.yaml', 'pubspec.lock')), 'B/C changes engine dependencies')
    require(len({value['bundleSha256'] for value in report['frameworkBinaries'].values()}) == 1,
            'A/B/C framework binary differs')
    builds = {}
    for name in 'ABC':
        directory = raw / ('build-' + name)
        bundle, compiler, pdf = (read(directory / filename) for filename in
                                 ('bundle.json', 'compiler-inputs.json', 'pdf-source-identity.json'))
        require(bundle['lib/libflutter_linux_gtk.so']['sha256'] == report['frameworkBinaries'][name]['bundleSha256'],
                'Bundle/framework receipt differs')
        for relative in (TARGET, APP[0]):
            matches = [checksum for path, checksum in compiler['inputs'].items() if path.endswith('/' + relative)]
            require(matches == [report['sources'][name]['files'][relative]], 'Compiler omitted the actual product/target source')
        for package_name, package in pdf['packages'].items():
            require(package_name in ARCHIVES and package['compiledInputs'], 'Unknown or uncompiled PDF package')
            require(all(compiler['inputs'].get(package['root'] + '/' + relative) == checksum
                        for relative, checksum in package['compiledInputs'].items()), 'Compiled PDF source receipt differs')
            if name == 'A':
                require(package['source'] == 'hosted' and package['archiveSha256'] == ARCHIVES[package_name],
                        'Baseline package archive differs')
            else:
                require(package['source'] == 'git' and package['commit'] == ENGINE, 'Backport package source differs')
        require(set(pdf['packages']) == set(ARCHIVES), 'Missing viewer or engine source receipt')
        builds[name] = {filename: digest(directory / filename) for filename in
                        ('bundle.json', 'compiler-inputs.json', 'pdf-source-identity.json')}
    return builds


def validate_launch_identity(directory, result, config, report, expected_pid):
    variant = config['variant']
    require(config['sourceCommit'] == report['plan'][variant], 'Launch source differs from fixed plan')
    expected_app = json_hash({path: report['sources'][variant]['files'][path] for path in APP})
    require(config['appSourcesSha256'] == expected_app, 'Compiled app-source digest differs')
    ready, native = read(directory / 'ready.json'), read(directory / 'native-identity.json')
    require(ready['pid'] == native['pid'] == result['pid'] == expected_pid
            and ready['sourceCommit'] == config['sourceCommit']
            and ready['appSourcesSha256'] == expected_app, 'Owned native/ready/result identity differs')
    require(native['identity'] == report['frameworkBinaries'][variant], 'Mapped framework receipt differs')
    require(native['pdfium']['sha256'] == config['pdfiumSha256'] == PDFIUM
            and native['pdfium']['canonicalPath'] == config['pdfium'], 'Mapped PDFium/input receipt differs')
    require(native['canonicalPath'] in native['rawObservation'], 'Mapped framework absent from raw process observation')
    require(native['pdfium']['mappings'] and all(row['path'] == config['pdfium'] and row['inode'] > 0
                for row in native['pdfium']['mappings']), 'Missing original PDFium ownership mapping')


def replay_group(spec, archive_dir, output, validator_path, validator_sha):
    directory = output / spec['id']
    members = extract_verified(archive_dir / (str(spec['artifactId']) + '.zip'), spec, directory)
    raw = directory / 'pdf-5538-font-profile'
    report = read(raw / 'report.json')
    builds = verify_source_receipts(raw, report, spec)
    require(len(report['rows']) == spec['originalValidatedRows'], 'Original completed row count differs')
    listed = {}
    for row in report['rows']:
        relative = PurePosixPath(*PurePosixPath(row['report']).parts[-3:]).as_posix()
        require(relative not in listed and relative.startswith(row['scenario'] + '/'), 'Duplicate/wrong original result row')
        listed[relative] = row
    results = sorted(raw.glob('*/*/result.json'))
    require(len(results) == spec['rawResultCount'], 'Raw result coverage differs')
    fixture_manifest = read(raw / 'fixtures/manifest.json')
    mapping = {}
    validation = load_validation_functions(validator_path, validator_sha, mapping)['validate_result']
    validated, pids = [], set()
    missing_original = []
    for path in results:
        relative = path.relative_to(raw).as_posix()
        result, config = read(path), read(path.with_name('config.json'))
        immutable = json_hash({'result': result, 'config': config})
        row = listed.get(relative)
        if row is not None:
            require(digest(path) == row['reportSha256'] and read(path.with_name('validated.json')) == row['verdict'],
                    'Original result/verdict/row receipt differs')
        else:
            require(relative in spec['allowedOriginallyUnvalidated'] and not path.with_name('validated.json').exists(),
                    'Unlisted or overwritten original validation result')
            missing_original.append(relative)
        pid = row['pid'] if row is not None else result['pid']
        require(pid not in pids, 'One process is being counted more than once within a workflow')
        pids.add(pid)
        validate_launch_identity(path.parent, result, config, report, pid)
        fixture_mapping(raw, config, fixture_manifest, mapping)
        verdict = validation(path.parent.resolve(), result, config, pid)
        if row is not None:
            require(verdict == row['verdict'], 'Full strict replay differs from an original verdict')
        require(json_hash({'result': result, 'config': config}) == immutable, 'Replay mutated original event/config data')
        validated.append({'path': relative, 'resultSha256': digest(path), 'pid': pid,
                          'variant': config['variant'], 'route': config['route'], 'scenario': config['scenario'],
                          'originallyValidated': row is not None,
                          'originalBlock': row['block'] if row is not None else 'unvalidated warmup',
                          'frameworkErrors': result['frameworkErrors'], 'frameCaptureErrors': result['frameCaptureErrors'],
                          'strictVerdict': verdict})
    require(sorted(missing_original) == sorted(spec['allowedOriginallyUnvalidated']), 'Original partial coverage differs')
    verify_members(directory, members)
    save(directory / 'replay-original-members.json', members)
    result = {'status': 'passed', 'scope': 'Post-validation only; original workflow conclusion retained',
              'runId': spec['runId'], 'jobId': spec['jobId'], 'artifactId': spec['artifactId'],
              'sourceCommit': spec['sourceCommit'], 'zipSha256': spec['zipSha256'],
              'originalWorkflowStatus': report['status'], 'originalError': report.get('error'),
              'originalMembersVerifiedUnchanged': len(members), 'memberManifestSha256': digest(directory / 'replay-original-members.json'),
              'originalReportSha256': digest(raw / 'report.json'), 'buildReceipts': builds,
              'strictValidatorSha256': validator_sha, 'rawResultsValidated': len(validated), 'rows': validated,
              'runtimeSourcesByVariant': {name: {path: source['files'][path] for path in RUNTIME}
                                          for name, source in report['sources'].items()},
              'originalCommonHarness': report['causalPlan']['commonHarness'],
              'limitations': ['The entire full validate_result function and helpers are loaded unchanged from SHA-pinned source.',
                              'Only the PdfReader fixture open maps original paths to verified archive PDFs; config/events and ownership comparisons are unchanged.',
                              'Archived native mappings and compiler/bundle receipts are checked, not a new live process or binary execution.',
                              'Baseline and built-in diagnostic correctness failures remain explicitly recorded, not converted to product success.',
                              'No timings are averaged or pooled across original workflow runs.']}
    save(directory / 'replay-result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--archives', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--validator', type=Path, default=Path(__file__).with_name('run_font_fallback_profile.py'))
    args = parser.parse_args()
    plan = read(args.plan)
    require(plan['schemaVersion'] == 1 and len(plan['groups']) == 2
            and re.fullmatch(r'[0-9a-f]{64}', plan['strictValidatorSha256']), 'Pinned replay plan is malformed')
    output = args.output.resolve()
    require(not output.exists(), 'Replay output must be new')
    output.mkdir(parents=True)
    summary = {'status': 'running', 'scope': 'Strict Python post-validation of frozen S02 evidence, no native remeasurement',
               'planSha256': digest(args.plan), 'strictValidatorSha256': plan['strictValidatorSha256'],
               'replaySourceSha256': digest(__file__), 'groups': []}
    try:
        for spec in plan['groups']:
            result = replay_group(spec, args.archives.resolve(), output, args.validator.resolve(), plan['strictValidatorSha256'])
            summary['groups'].append({key: result[key] for key in
                                      ('runId', 'artifactId', 'zipSha256', 'originalWorkflowStatus', 'rawResultsValidated')})
            if len(summary['groups']) == 1:
                runtime, first_harness = result['runtimeSourcesByVariant'], result['originalCommonHarness']
            else:
                require(result['runtimeSourcesByVariant'] == runtime, 'Runtime app/engine inputs changed between the two workflows')
                require({path for path in first_harness if first_harness[path] != result['originalCommonHarness'][path]} == {HOST},
                        'Cross-workflow common measured harness differs beyond the declared host validator change')
        require(sum(group['rawResultsValidated'] for group in summary['groups']) == 95, 'Expected all 83 + 12 raw processes')
        summary.update(status='passed', rawResultsValidated=95, originalRawUnchanged=True,
                       crossRunTimingPooling=False, originalWorkflowConclusionsRetained=True)
    except BaseException as error:
        summary.update(status='failed', error=repr(error))
        raise
    finally:
        save(output / 'replay-summary.json', summary)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()

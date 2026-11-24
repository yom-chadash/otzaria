import argparse
from contextlib import nullcontext
from collections import Counter
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import zipfile

WORKSPACE = None
BASE = 'ac0237f34011d823a856117834a18cf02a64b774'
FORK = 'https://github.com/yom-chadash/otzaria'
MAX_PART_BYTES = 1_500_000
TEXT_EXTENSIONS = {'.json', '.jsonl', '.csv', '.log', '.patch', '.diff', '.lock', '.txt', '.tap', '.d', '.xml'}
BUILD_RECEIPTS = {'build-command.json', 'bundle.json', 'compiler-inputs.json', 'resolution.json', 'source.json'}
SIGNED = re.compile(rb'(?i)(?:[?&](?:sig|signature|x-amz-signature|x-goog-signature|token|access_token)=|github_pat_|ghp_[A-Za-z0-9]{20})')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def included(path):
    if 'data' in path.parts[:-1] and any(p in {'app-fixture', 'pinned-store'} for p in path.parts[:-1]):
        return False, 'generated-application-data; scenario reports and source identities retained'
    if any(p in {'engine-original', 'engine', '.dart_tool', 'node_modules', '.git', 'runtime', '.pub-cache'} for p in path.parts[:-1]):
        return False, 'dependency-build-or-runtime-copy'
    if 'build' in path.parts[:-1] and path.name not in BUILD_RECEIPTS and path.suffix not in {'.log', '.d', '.lock'}:
        return False, 'build-tree-content-other-than-provenance-receipts'
    if path.suffix == '.pdf':
        if (len(path.parts) == 4 and path.parts[:2] == ('pdf-5538-font-profile', 'fixtures')
                and re.fullmatch(r'(source-\d+|unrelated)\.pdf', path.name)):
            return True, None
        return False, 'pdf-input-or-generated-fixture; identities retained in reports and corpus catalog'
    if path.suffix in {'.zip', '.gz'}:
        return False, 'original-container-or-dependency-archive; container identity retained'
    if re.fullmatch(r'frame-\d+\.png', path.name):
        return True, None
    if path.suffix in TEXT_EXTENSIONS or path.name in {'start', 'stop'}:
        return True, None
    return False, 'binary-or-redundant-source; source and binary identities retained in receipts'


def validate_font_fixture(path, payload):
    root = path.parent.parent
    manifest = read_json(root / 'manifest.json')
    relative = path.relative_to(root).as_posix()
    matches = [item for case in manifest['cases'] for item in case['documents'] if item['path'] == relative]
    assert len(matches) == 1, 'Font fixture must appear once in its original manifest'
    item = matches[0]
    assert payload.startswith(b'%PDF-') and len(payload) <= 1024 * 1024, 'Unexpected font fixture type/size'
    assert len(payload) == item['sizeBytes'] and sha(payload) == item['sha256'], 'Font fixture changed'
    assert not SIGNED.search(payload), 'Unexpected credential-like text in generated fixture'


def validate_frame_png(path, payload, *, incomplete_launch=False):
    assert payload.startswith(b'\x89PNG\r\n\x1a\n') and payload[12:16] == b'IHDR', 'Invalid frame PNG'
    dimensions = (int.from_bytes(payload[16:20], 'big'), int.from_bytes(payload[20:24], 'big'))
    assert len(payload) <= 32 * 1024 * 1024 and all(0 < size <= 8192 for size in dimensions), 'Unexpected frame bounds'
    receipt = path.parent / 'frame-receipts.json'
    sidecar = read_json(receipt) if receipt.is_file() else {'measured': [], 'reference': []}
    rows = [row for row in sidecar['measured'] + sidecar['reference'] if row['pngPath'] == path.name]
    if rows:
        assert all(row['pngSha256'] == sha(payload) for row in rows), 'Changed frame PNG'
        assert all(dimensions == (row['width'], row['height']) for row in rows), 'Frame PNG dimensions changed'
        return {'status': 'listed-in-original-frame-receipts', 'width': dimensions[0], 'height': dimensions[1]}
    assert incomplete_launch and not (path.parent / 'result.json').exists(), 'Unreferenced frame PNG'
    return {'status': 'unvalidated-capture-from-incomplete-launch', 'width': dimensions[0], 'height': dimensions[1],
            'completedFrameReceipt': False, 'firstGoodOrReferenceEvidence': False}


def group(source_root, relative, run, job, artifact, commit, classification, expected_sha=None, summary=None, supplementary=None, artifact_name=None, job_log=None, job_log_sha256=None, job_log_newline_normalization=None):
    root = WORKSPACE / source_root / relative
    name = Path(relative).name
    if artifact is not None:
        archives = sorted(root.glob('*.zip'))
        if not archives:
            # A version suffix such as "3.50.0" is part of the artifact name.
            candidates = dict.fromkeys([root.with_name(root.name + '.zip'), root.with_suffix('.zip')])
            archives = [candidate for candidate in candidates if candidate.is_file()]
        assert len(archives) == 1 and archives[0].is_file(), root
    else:
        assert job_log is not None, 'A group needs an artifact or a decoded job log'
        archives = [None]
    if job_log is not None:
        relative_log = Path(job_log)
        assert not relative_log.is_absolute() and '..' not in relative_log.parts and '\\' not in job_log and ':' not in job_log
        assert (root / relative_log).is_file() and relative_log.suffix == '.log'
        job_log_sha256 = job_log_sha256 or (expected_sha if artifact is None else None)
        assert job_log_sha256 and re.fullmatch(r'[0-9a-f]{64}', job_log_sha256)
        assert job_log_newline_normalization in {'LF', 'none; connector text already LF', 'not recorded; saved connector text'}
    if summary is None:
        choices = sorted(root.rglob('analysis-summary.json'))
        assert len(choices) == 1, (root, choices)
        summary = choices[0]
    elif summary not in {'first-attempt', 'preflight'}:
        summary = root / summary
        assert summary.is_file(), summary
    return dict(source_root=source_root, relative=relative, root=root, name=name,
        run=run, job=job, artifact=artifact, commit=commit, classification=classification,
        archive=archives[0], expected_sha=expected_sha, summary=summary, supplementary=supplementary or [], job_log=job_log,
        job_log_sha256=job_log_sha256, job_log_newline_normalization=job_log_newline_normalization,
        artifact_name=artifact_name or ('pdf-5538-preflight-linux' if name == 'preflight' else name))


def inventory():
    c = 'pdf-5538-ci-evidence'
    u = 'ci-upstream-audit'
    b83 = 'b83d51a08fdc3eab12259ca134166743fc17e2b7'
    campaign = '27e662d2e64db076d19f9ccc22c9b52a8619ea45'
    copy = '467811a97c93aa2b3b8879fc1b443db1b50f2d4e'
    current = 'fc81d39cfe5f50f38c26a1341fd8fae86330ad20'
    blocked = 'dc700936532ff990c8112a3a57ff5957cf1b3ae6'
    groups = [
        group(c, 'pdf-5538-copy-letteris-hebrew-1910', 45393975596, 138444732482, 14920857004, copy, 'completed-measurement'),
        group(c, 'pdf-5538-copy-mikraot-gedolot-genesis-1859', 45393975596, 138444732077, 14919284300, copy, 'completed-measurement'),
        group(c, 'pdf-5538-copy-uxlc-genesis', 45393975596, 138444732348, 14919494266, copy, 'completed-measurement'),
        group(c, 'pdf-5538-highlights', 45392015952, 138441654238, 14921060122, campaign, 'completed-measurement'),
        group(c, 'pdf-5538-internals-linux-x64', 45389994425, 138432215506, 14919615066, b83, 'completed-measurement'),
        group(c, 'pdf-5538-large-export', 45392015952, 138441654345, 14918954604, campaign, 'completed-measurement'),
        group(c, 'pdf-5538-text-mikraot-gedolot-genesis-1859', 45389994425, 138432215508, 14919056683, b83, 'completed-measurement'),
        group(c, 'pdf-5538-text-uxlc-genesis', 45389994425, 138432215536, 14917533590, b83, 'completed-measurement',
              'dc88b4af983e425e7144a7e22b737e96f9f5457d34bfc5d5a9cadf6bc12cab02'),
        group(c, 'pdf-5538-upstream-first-attempt', 45389994425, 138432215296, 14917654912, b83, 'partial-correctness-failed-measurement',
              'c49dfad9049536035dcc8c500ce07cdab3a8caa31e5ba75e939d91c9fe549c33', summary='first-attempt'),
        group(c, 'preflight', 45386082648, 138417219879, 14916960335, '4c2ec82c2c98d9be61816b80690f8e385c9f7c8c', 'environment-preflight',
              '7148d0ccdb128749cd97a8f9efb240ac70f319e49b5c39b63917b0b804c76509', summary='preflight'),
        group(u, '45392015633/pdf-5538-upstream', 45392015633, 138442107484, 14920006621, campaign, 'blocked-before-measurement'),
        group(u, '45392759336/pdf-5538-upstream-suites-current-stable-canary', 45392759336, 138440491390, 14919123007, blocked, 'blocked-before-measurement'),
        group(u, '45392759336/pdf-5538-upstream-suites-fixed-3.50.0', 45392759336, 138440491671, 14918584407, blocked, 'blocked-before-measurement'),
        group(u, '45394319292/pdf-5538-upstream-suites-current-stable-canary', 45394319292, 138445907008, 14919469717, current, 'completed-upstream-correctness',
              '82b2d9c6b787e235e5fabfa08b86aec5e7a565f7890aed814961ca9a2394f7d0'),
        group(u, '45394319292/pdf-5538-upstream-suites-fixed-3.50.0', 45394319292, 138445907230, 14920097749, current, 'completed-upstream-correctness',
              '594cab2f40a808101dd2464cce3b4ee33e435b35327d53c43b0e8cb5ae5ac987'),
        group(u, '45394319381/pdf-5538-upstream', 45394319381, 138445906919, 14920466763, current, 'completed-measurement'),
    ]
    return sorted(groups, key=lambda g: (g['run'], g['name']))


def summary_for(g):
    if g['summary'] == 'first-attempt':
        return {'schemaVersion': 1, 'status': 'partial-correctness-failed-measurement',
            'runId': g['run'], 'jobId': g['job'], 'artifactId': g['artifact'], 'sourceCommit': g['commit'],
            'jobUrl': f"{FORK}/actions/runs/{g['run']}/job/{g['job']}",
            'completedSuites': read_json(g['root'] / 'suite-receipts.json'),
            'measurementProbesCompleted': False, 'productImprovementClaim': False,
            'failure': read_json(g['root'] / 'failure.json'),
            'interpretationHe': 'בדיקות native, Chrome ו־Node הסתיימו, ואז source guard נכשל על registrants שנוצרו. הפרובים לא רצו; זה אינו סבב מדידות שהושלם.',
            'rawEvidence': ['suite-receipts.json', 'native-suites.jsonl', 'chrome-suites.jsonl', 'node-suites.log', 'failure.json']}
    if g['summary'] == 'preflight':
        return {'schemaVersion': 1, 'status': 'environment-preflight', 'runId': g['run'], 'jobId': g['job'],
            'artifactId': g['artifact'], 'sourceCommit': g['commit'], 'pdfMeasurement': False,
            'jobUrl': f"{FORK}/actions/runs/{g['run']}/job/{g['job']}",
            'jobConclusion': 'success', 'runner': 'ubuntu-24.04',
            'jobMetadataSource': f"https://api.github.com/repos/yom-chadash/otzaria/actions/runs/{g['run']}/jobs?per_page=30",
            'report': read_json(g['root'] / 'pdf-5538-preflight.json')}
    return read_json(g['summary'])


def expected_zip_sha(g, summary):
    if g['expected_sha']:
        return g['expected_sha']
    for value in [summary, summary.get('evidence', {}), summary.get('ci', {})]:
        for key in ['archiveSha256', 'artifactSha256', 'artifactZipSha256']:
            if key in value:
                return value[key]
    raise ValueError('No expected artifact digest: ' + g['name'])


def archive_group(g, destination, immutable_sources):
    summary = summary_for(g)
    zip_bytes = g['archive'].read_bytes() if g['archive'] is not None else None
    zip_sha = sha(zip_bytes) if zip_bytes is not None else None
    if zip_bytes is not None:
        assert zip_sha == expected_zip_sha(g, summary), ('artifact digest mismatch', g['archive'])
        immutable_sources[g['archive']] = zip_sha
    log_bytes = None
    if g['job_log'] is not None:
        log_path = g['root'] / g['job_log']
        log_bytes = log_path.read_bytes()
        assert sha(log_bytes) == g['job_log_sha256'], 'Decoded job log digest mismatch'
        if 'jobLogSha256' in summary:
            assert sha(log_bytes) == summary['jobLogSha256']
        if zip_bytes is None:
            assert (summary['runId'], summary['jobId'], summary['sourceCommit']) == (g['run'], g['job'], g['commit'])
        for item in summary.get('evidenceFiles', []):
            if item['path'] == g['job_log']:
                assert item['sha256'] == sha(log_bytes) and item['bytes'] == len(log_bytes)
        if g['job_log_newline_normalization'] in {'LF', 'none; connector text already LF'}:
            assert b'\r' not in log_bytes, 'Job log is not LF normalized'
        immutable_sources[log_path] = sha(log_bytes)
    key = f"{g['run']}-{g['name']}"
    raw_files = []
    omitted = []
    suffix_counts = Counter()
    content = {}
    with (zipfile.ZipFile(io.BytesIO(zip_bytes)) if zip_bytes is not None else nullcontext()) as original:
        members = ({i.filename: i for i in original.infolist() if not i.is_dir()}
                   if original is not None else {})
        assert g['job_log'] not in members, 'Decoded job log must not replace an artifact member'
        for path in sorted(g['root'].rglob('*')):
            if not path.is_file():
                continue
            assert not path.is_symlink(), ('symlink', path)
            relative = path.relative_to(g['root'])
            selected, reason = included(relative)
            if not selected:
                omitted.append({'path': relative.as_posix(), 'bytes': path.stat().st_size, 'reason': reason})
                continue
            payload = path.read_bytes()
            frame_evidence = None
            if path.suffix == '.pdf':
                validate_font_fixture(path, payload)
            elif path.suffix == '.png':
                incomplete_launch = (g['classification'] == 'partial-harness-gate-failure' and original is not None
                    and len(relative.parts) == 4 and relative.parts[0] == 'pdf-5538-font-profile')
                frame_evidence = validate_frame_png(path, payload, incomplete_launch=incomplete_launch)
            else:
                payload.decode('utf-8')
                assert not SIGNED.search(payload), ('signed URL or token candidate', path)
            digest = sha(payload)
            immutable_sources[path] = digest
            name = relative.as_posix()
            origin = 'post-download-analysis-or-receipt'
            if name == g['job_log']:
                assert payload == log_bytes
                origin = 'github-connector-decoded-job-log'
            elif name in members:
                assert sha(original.read(name)) == digest, ('extracted member mismatch', path)
                origin = 'original-github-artifact-member'
            else:
                assert path.name in {'analysis-summary.json', 'download-receipt.json', 'artifact-receipt.json'}, ('unexplained local file', path)
            item = {'path': name, 'bytes': len(payload), 'sha256': digest, 'origin': origin}
            if frame_evidence is not None:
                item['frameEvidence'] = frame_evidence
            if path.suffix == '.jsonl':
                records = 0
                other_lines = []
                for number, line in enumerate(payload.splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        json.loads(line)
                        records += 1
                    except json.JSONDecodeError:
                        other_lines.append(number)
                if path.name == 'observations.jsonl':
                    assert not other_lines, ('invalid observation JSONL', path, other_lines)
                item['jsonlRows'] = records
                if other_lines:
                    item['nonJsonOutputLineNumbersPreservedVerbatim'] = other_lines
            if path.suffix == '.json':
                try:
                    json.loads(payload)
                except json.JSONDecodeError:
                    # The product CI names the Dart JSON reporter stream .json.
                    # Preserve its original bytes and require valid JSONL events.
                    if path.name not in {'pdf-product-tests.json', 'pdf-font-tests.json'}:
                        raise
                    events = [json.loads(line) for line in payload.splitlines() if line.strip()]
                    assert events and all(isinstance(event, dict) and isinstance(event.get('type'), str) for event in events)
                    item['contentFormat'] = 'jsonl-despite-json-extension'
                    item['jsonlRows'] = len(events)
            raw_files.append(item)
            suffix_counts[path.suffix or '<extensionless-text>'] += 1
            content[name] = payload
    for relative in g['supplementary']:
        path = (WORKSPACE / relative).resolve()
        assert path.is_relative_to(WORKSPACE) and path.is_file() and not path.is_symlink(), path
        payload = path.read_bytes()
        payload.decode('utf-8')
        assert not SIGNED.search(payload), ('signed URL or token candidate', path)
        name = 'post-download-analysis/' + path.name
        assert name not in content, 'Duplicate supplementary analysis file'
        digest = sha(payload)
        immutable_sources[path] = digest
        content[name] = payload
        raw_files.append({'path': name, 'bytes': len(payload), 'sha256': digest,
            'origin': 'post-download-analysis-code', 'originalLocation': path.relative_to(WORKSPACE).as_posix()})
        suffix_counts[path.suffix] += 1
    raw_files.sort(key=lambda item: item['path'])
    stream = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=stream, compresslevel=9, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.USTAR_FORMAT) as tar:
            for name, payload in sorted(content.items()):
                header = tarfile.TarInfo(name)
                header.size = len(payload)
                header.mode = 0o644
                header.mtime = header.uid = header.gid = 0
                header.uname = header.gname = ''
                tar.addfile(header, io.BytesIO(payload))
    archive = stream.getvalue()
    parts = []
    total_parts = (len(archive) + MAX_PART_BYTES - 1) // MAX_PART_BYTES
    for index in range(total_parts):
        part = archive[index * MAX_PART_BYTES:(index + 1) * MAX_PART_BYTES]
        suffix = '' if total_parts == 1 else f'.part-{index + 1:03d}'
        name = f'raw/{key}.tar.gz{suffix}'
        path = destination / name
        path.parent.mkdir(exist_ok=True)
        assert not path.exists(), ('refusing to replace an archive part', path)
        path.write_bytes(part)
        parts.append({'path': name, 'bytes': len(part), 'sha256': sha(part)})
    summary_path = f'summaries/{key}.json'
    assert not (destination / summary_path).exists(), 'Refusing to replace a summary'
    write_json(destination / summary_path, summary)
    return {'id': key, 'classification': g['classification'],
        'declaredScopeComplete': g['classification'] in {
            'completed-measurement', 'completed-upstream-correctness',
            'completed-app-correctness', 'completed-product-regression-checks'},
        'ci': {'repository': 'yom-chadash/otzaria', 'runId': g['run'], 'jobId': g['job'],
            'jobUrl': f"{FORK}/actions/runs/{g['run']}/job/{g['job']}", 'sourceCommit': g['commit'],
            'sourceUrl': f"{FORK}/tree/{g['commit']}",
            'artifact': {'id': g['artifact'], 'name': g['artifact_name'],
                'url': f"{FORK}/actions/runs/{g['run']}/artifacts/{g['artifact']}",
                'zipSha256': zip_sha, 'zipBytes': len(zip_bytes), 'expectedDigestVerified': True,
                'originalZipStoredAgain': False} if zip_bytes is not None else None,
            **({'jobLog': {'path': g['job_log'], 'sha256': sha(log_bytes), 'bytes': len(log_bytes),
                          'origin': 'GitHub connector decoded job log',
                          'newlineNormalization': g['job_log_newline_normalization'],
                          'originalActionsLogZip': False,
                          'expectedDigestVerified': True, 'artifactWasCreated': zip_bytes is not None}} if log_bytes is not None else {})},
        'originalLocation': f"{g['source_root']}/{g['relative']}",
        'summary': {'path': summary_path, 'sha256': sha((destination / summary_path).read_bytes()),
            'derivedFrom': str(g['summary'].relative_to(g['root'])) if isinstance(g['summary'], Path) else g['summary']},
        'rawArchive': {'format': 'tar+gzip', 'sha256': sha(archive), 'bytes': len(archive),
            'partsInConcatenationOrder': parts, 'files': raw_files, 'fileCount': len(raw_files),
            'uncompressedBytes': sum(f['bytes'] for f in raw_files), 'fileTypes': dict(sorted(suffix_counts.items())),
            'jsonlRows': sum(f.get('jsonlRows', 0) for f in raw_files)},
        'omitted': omitted}


def main():
    global WORKSPACE
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True,
                        help='Directory containing the downloaded source groups.')
    parser.add_argument('--catalog', type=Path,
                        help='Pinned corpus_sources.json, required for a new archive.')
    parser.add_argument('--restore-tool', type=Path, default=Path(__file__).with_name('restore.py'))
    parser.add_argument('--append-plan', type=Path,
                        help='JSON array of new group() arguments; existing group ids and raw parts are immutable.')
    args = parser.parse_args()
    WORKSPACE = args.workspace.resolve()
    output = args.output.resolve()
    existing_groups = []
    if args.append_plan:
        previous_bytes = (output / 'manifest.json').read_bytes()
        assert sha(previous_bytes) == (output / 'manifest.sha256').read_text().split()[0], 'Existing manifest changed'
        previous = json.loads(previous_bytes)
        existing_groups = previous['groups']
        for old in existing_groups:
            for receipt in [old['summary'], *old['rawArchive']['partsInConcatenationOrder']]:
                assert sha((output / receipt['path']).read_bytes()) == receipt['sha256'], 'Existing evidence changed'
        planned = [group(**record) for record in read_json(args.append_plan)]
        catalog = output / 'corpus-provenance.json'
    else:
        assert not output.exists(), 'Output must be new unless --append-plan is supplied'
        assert args.catalog, '--catalog is required for a new archive'
        output.mkdir(parents=True)
        planned = inventory()
        catalog = args.catalog.resolve()
    planned_ids = [f"{g['run']}-{g['name']}" for g in planned]
    assert len(set(planned_ids)) == len(planned_ids), 'Duplicate new group ids'
    assert not (set(planned_ids) & {g['id'] for g in existing_groups}), 'Cannot overwrite an existing evidence group'
    catalog_bytes = catalog.read_bytes()
    (output / 'corpus-provenance.json').write_bytes(catalog_bytes)
    immutable_sources = {catalog: sha(catalog_bytes)}
    groups = sorted([*existing_groups, *(archive_group(g, output, immutable_sources) for g in planned)], key=lambda g: g['id'])
    tools = []
    for source, name in [(Path(__file__), 'build.py'), (args.restore_tool, 'restore.py')]:
        data = source.read_bytes()
        (output / name).write_bytes(data)
        tools.append({'path': name, 'sha256': sha(data), 'bytes': len(data)})
    counts = dict(sorted(Counter(g['classification'] for g in groups).items()))
    corrections = []
    for entry in groups:
        if entry['id'].endswith('-pdf-5538-http-revalidation-linux-x64'):
            http = read_json(output / entry['summary']['path'])
            phases = [http['phaseCounts'][name] for name in ['warm', 'changed-warm']]
            corrections.append({'topic': 'E10 HTTP cache reuse',
                'earlierGroup': '45389994425-pdf-5538-internals-linux-x64',
                'followupGroup': entry['id'], 'sourceSummary': entry['summary'],
                'warmFirstPageObservations': sum(p['observations'] for p in phases),
                'zeroPayloadObservations': sum(p['zeroPayload'] for p in phases),
                'validated304Observations': sum(p['with304'] for p in phases),
                'noRequestObservations': sum(p['noRequests'] for p in phases),
                'independentProcesses': http['independentProcesses'],
                'interpretation': 'The locked engine reuses cached bytes when the server implements real conditional HTTP. The earlier unconditional-server redownload result must not be generalized to all warm opens.',
                'scope': 'Loopback backend observations across four AOT processes; changed resources were tested after stale headers. No WAN or UI performance claim.'})
    manifest = {'schemaVersion': 1, 'snapshotDate': '2026-11-24',
        'archivePurpose': 'Permanent fork-only evidence snapshot for Otzaria issue #5538',
        'issueUrl': 'https://github.com/Otzaria/otzaria/issues/5538', 'repository': FORK,
        'sourceBranch': 'perf/5538-final-measurements-20261124', 'archiveBaseCommit': BASE,
        'scope': 'Explicit snapshot of the listed artifact groups. Later CI artifacts are added as separate evidence, not inferred as complete.',
        'groupCountsByClassification': counts,
        'interpretationCaveats': [
            'Completion applies to the declared producer/job scope; an independent failure elsewhere in the workflow is not a failure of this measurement.',
            'A completed measurement may demonstrate an unfixed defect; completion does not imply a product fix or product correctness.',
            'Failed and blocked attempts are retained but are never counted as completed measurement probes.',
            'Upstream suites use their recorded isolated SDK. They do not demonstrate adoption by Otzaria on its locked SDK.',
            'The original E10 loopback server did not implement 304. Its warm redownload observations do not establish behavior with a conditional HTTP server.',
            'Repeated passes within one process are not independent launches; see each summary for sample sizes and timing limits.',
            'Connector-decoded job logs are labelled separately from byte-verified artifact ZIP members; any recorded newline normalization is explicit.',
            'Selected S02 frame PNGs retain their original artifact bytes and are checked against frame-receipts.json; a frame capture is not an OS presentation timestamp.',
            'Small S02 synthetic PDF inputs are retained only under their fixture paths and verified against the original fixture manifest and artifact ZIP; real-book corpus bytes remain excluded.',
            'PdfPageHighlights is an advanced-search component. Its literal String case is a component microbenchmark, not a measured acceleration of simple product search. Simple and /regex/ searches use pdfrx.startTextSearch; the advanced-search RegExp fallback is a product path. ResultLines follow-up is mixed and supports neither an improvement nor a non-regression claim.',
            'No PDF corpus, runtime binary, dependency checkout, build tree or signed download URL is embedded in these raw archives.'
        ],
        'highlightProductScopeSources': [
            f'{FORK}/blob/{BASE}/lib/pdf_book/view/pdf_search_screen.dart',
            f'{FORK}/blob/{BASE}/lib/pdf_book/utils/centered_text_search_session.dart'
        ],
        'corpusProvenance': {'path': 'corpus-provenance.json', 'sha256': sha(catalog_bytes),
            'source': f'{FORK}/blob/{BASE}/tool/issue5538_ci/corpus_sources.json',
            'textRetention': 'Extracted public texts are retained verbatim with their source attribution, license and original PDF SHA-256 in this catalog and per-run receipts.'},
        'reproducibleContainer': {'memberOrder': 'UTF-8 relative path, lexical order', 'tarFormat': 'USTAR',
            'memberType': 'regular files only', 'memberMode': '0644', 'uid': 0, 'gid': 0, 'mtime': 0,
            'uname': '', 'gname': '', 'gzipFilename': '', 'gzipMtime': 0, 'gzipCompressionLevel': 9,
            'maxPartBytes': MAX_PART_BYTES, 'parts': 'Concatenate partsInConcatenationOrder byte-for-byte before gzip/tar decoding.'},
        'restoration': {'tool': 'restore.py',
            'verifyCommand': 'python3 restore.py --verify',
            'restoreCommand': 'python3 restore.py --output /absolute/path/to/new-evidence-directory',
            'procedure': ['Verify every part SHA-256 and byte length.', 'Concatenate each group parts in manifest order; verify the joined SHA-256.',
                'Read gzip and tar, allowing only the listed regular files and relative paths.',
                'Verify each restored file SHA-256 and byte length. Each group is restored into its own id directory.'],
            'rawFilesUnmodified': True, 'originalArtifactMembersVerifiedAgainstZip': True,
            'postDownloadAnnotationsLabelledSeparately': True},
        'incrementalMaintenance': {'tool': 'build.py',
            'appendCommand': 'python3 build.py --workspace /download-root --output . --append-plan /absolute/new-groups.json',
            'planFormat': 'JSON array; one object per new group. Existing ids cannot be replaced.',
            'requiredFields': ['source_root', 'relative', 'run', 'job', 'artifact', 'commit', 'classification', 'expected_sha'],
            'optionalFields': {'artifact_name': 'Original GitHub artifact name when the local group directory has a different name.',
                'job_log': 'Relative path to saved connector-decoded job log, alongside an artifact or alone with artifact null.',
                'job_log_sha256': 'Required decoded job log digest when an artifact is also present; otherwise defaults to expected_sha.',
                'job_log_newline_normalization': 'Explicit LF, none; connector text already LF, or not recorded; saved connector text. No original Actions log ZIP identity is implied.',
                'summary': 'Relative path to analysis-summary.json; discovered automatically if omitted.',
                'supplementary': 'Explicit list of post-download analysis code paths relative to --workspace; labelled separately from CI artifact members.'},
            'source_root': 'Path relative to --workspace, such as pdf-5538-ci-evidence.',
            'relative': 'Group directory relative to source_root.',
            'existingRawPartsAreImmutable': True},
        'tools': tools,
        'groups': groups}
    manifest['evidenceCorrections'] = corrections
    overview = {'schemaVersion': 1, 'snapshotDate': '2026-11-24', 'groupCountsByClassification': counts,
        'groups': [{k: g[k] for k in ['id', 'classification', 'declaredScopeComplete', 'ci', 'summary']} |
            {'rawFiles': g['rawArchive']['fileCount'], 'rawJsonlRows': g['rawArchive']['jsonlRows'],
             'compressedBytes': g['rawArchive']['bytes'], 'parts': len(g['rawArchive']['partsInConcatenationOrder'])} for g in groups],
        'interpretationCaveats': manifest['interpretationCaveats'], 'evidenceCorrections': corrections}
    write_json(output / 'overview.json', overview)
    manifest['overview'] = {'path': 'overview.json', 'sha256': sha((output / 'overview.json').read_bytes())}
    write_json(output / 'manifest.json', manifest)
    (output / 'manifest.sha256').write_text(sha((output / 'manifest.json').read_bytes()) + '  manifest.json\n')
    for source, expected in immutable_sources.items():
        assert sha(source.read_bytes()) == expected, ('source changed during archival', source)
    print(json.dumps({'groups': len(groups), 'counts': counts, 'rawFiles': sum(g['rawArchive']['fileCount'] for g in groups),
        'rawBytes': sum(g['rawArchive']['uncompressedBytes'] for g in groups),
        'compressedBytes': sum(g['rawArchive']['bytes'] for g in groups),
        'archiveParts': sum(len(g['rawArchive']['partsInConcatenationOrder']) for g in groups),
        'manifestSha256': sha((output / 'manifest.json').read_bytes())}, ensure_ascii=False))


if __name__ == '__main__':
    main()

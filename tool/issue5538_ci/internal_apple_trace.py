"""Capture thread samples after the unprofiled Apple comparison has finished."""

from collections import Counter
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def native_threads(path, expected_process, backend):
    """Resolve xctrace XML references; preserve raw XML for independent review."""
    tree = ET.parse(path)
    elements = list(tree.iter())
    references = {e.get('id'): e for e in elements if e.get('id')}

    def resolved(element):
        visited = set()
        while element is not None and element.get('ref'):
            key = element.get('ref')
            if key in visited or key not in references:
                raise ValueError('Unresolved or cyclic xctrace XML reference: ' + key)
            visited.add(key)
            element = references[key]
        return element

    counts = {}
    row_count = 0
    other_process_rows = 0
    unidentifiable_rows = 0
    native_pattern = (r'\bFPDF\w*\b' if backend == 'pdfium' else
                      r'\b(?:CGPDF\w*|CGContextDrawPDFPage)\b|[-+]\[PDF(?:Page|Document|Selection)\b')
    for row in (e for e in elements if e.tag == 'row'):
        row_count += 1
        thread = resolved(next((e for e in row if e.tag == 'thread'), None))
        stack = resolved(next((e for e in row if e.tag == 'backtrace'), None))
        if thread is None or stack is None:
            unidentifiable_rows += 1
            continue
        process = resolved(thread.find('process'))
        if process is None:
            process = resolved(row.find('process'))
        if process is None:
            unidentifiable_rows += 1
            continue
        process_name = resolved(process.find('process-name'))
        name = process_name.text if process_name is not None else None
        process_label = process.get('fmt') or ''
        if name != expected_process and process_label != expected_process and not process_label.startswith(expected_process + ' ('):
            other_process_rows += 1
            continue
        tid = resolved(thread.find('tid'))
        pid = resolved(process.find('pid'))
        label = thread.get('fmt') or thread.get('name') or thread.get('id')
        key = (label, tid.text if tid is not None else None, process_label, pid.text if pid is not None else None)
        entry = counts.setdefault(key, {'samples': 0, 'nativeSamples': 0, 'frames': Counter()})
        entry['samples'] += 1
        native = []
        for node in stack.iter('frame'):
            frame = resolved(node)
            symbol = frame.get('name') or frame.get('fmt') or (frame.text or '').strip()
            if re.search(native_pattern, symbol):
                native.append(symbol)
        if native:
            entry['nativeSamples'] += 1
            entry['frames'].update(set(native))
    return {'rows': row_count, 'otherProcessRows': other_process_rows,
            'unidentifiableRows': unidentifiable_rows,
            'threadSamples': sum(v['samples'] for v in counts.values()),
            'nativeSymbolSamples': sum(v['nativeSamples'] for v in counts.values()),
            'threads': [{'label': label, 'tid': tid, 'process': process_label, 'pid': pid, 'samples': v['samples'],
                         'nativeSymbolSamples': v['nativeSamples'],
                         'nativeFrames': dict(v['frames'].most_common())}
                        for (label, tid, process_label, pid), v in counts.items()]}


def capture(*, executable, app, output, documents, manifest, source_commit,
            provenance_sha256, run, validate, digest, require, write_json):
    traces = output / 'traces'
    traces.mkdir()
    executable_sha = digest(executable)
    version = run(['xcrun', 'xctrace', 'version'], app, traces / 'xctrace-version.log', timeout=60)
    run(['xcodebuild', '-version'], app, traces / 'xcode-version.log', timeout=60)
    help_text = run(['xcrun', 'xctrace', 'help', 'record'], app, traces / 'record-help.log', timeout=60)
    require(all(flag in help_text for flag in ('--env', '--launch', '--no-prompt', '--time-limit')),
            'Installed xctrace does not support the required noninteractive launch contract')
    templates = run(['xcrun', 'xctrace', 'list', 'templates'], app, traces / 'templates.log', timeout=60)
    require('Time Profiler' in templates, 'Time Profiler is unavailable on this runner')
    selected = [d for d in documents if d['id'] in ('uxlc-genesis', 'mikraot-gedolot-genesis-1859')]
    require(len(selected) == 2, 'Missing real text and scan/OCR corpus for the thread trace')
    results = []
    for document in selected:
        input_path = (manifest.parent / document['path']).resolve()
        require(digest(input_path) == document['sha256'], 'Trace corpus identity changed')
        for backend in ('pdfium', 'coregraphics'):
            label = f"{document['id']}-{backend}"
            trace_path = traces / f'{label}.trace'
            config = {'backend': backend, 'workload': 'file', 'input': str(input_path),
                      'inputSha256': document['sha256'], 'output': str(traces / f'{label}-report.json')}
            config_path = traces / f'{label}-config.json'
            write_json(config_path, config)
            command = ['xcrun', 'xctrace', 'record', '--template', 'Time Profiler',
                       '--output', trace_path, '--time-limit', '90s', '--no-prompt',
                       '--env', f'ISSUE5538_CONFIG={config_path}', '--launch', '--', executable]
            write_json(traces / f'{label}-command.json', [str(value) for value in command])
            run(command, app, traces / f'{label}-record.log', timeout=180)
            require(trace_path.exists(), 'xctrace did not create its recording')
            report = json.loads(Path(config['output']).read_text())
            validate(report, config)
            require(digest(executable) == executable_sha, 'Trace changed the executable identity')
            toc = traces / f'{label}-toc.xml'
            run(['xcrun', 'xctrace', 'export', '--input', trace_path, '--toc', '--output', toc],
                app, traces / f'{label}-toc.log', timeout=120)
            exports = []
            schemas = []
            exported = set()
            for recording in ET.parse(toc).getroot().iter('run'):
                number = recording.get('number', '')
                require(number.isdecimal(), 'Unexpected xctrace recording number')
                for table in recording.findall('./data/table'):
                    schema = table.get('schema')
                    schemas.append(schema)
                    if schema not in ('time-profile', 'time-sample') or (number, schema) in exported:
                        continue
                    exported.add((number, schema))
                    xml = traces / f'{label}-{number}-{schema}.xml'
                    selector = f'/trace-toc/run[@number="{number}"]/data/table[@schema="{schema}"]'
                    run(['xcrun', 'xctrace', 'export', '--input', trace_path, '--xpath', selector,
                         '--output', xml], app, traces / f'{label}-{number}-{schema}.log', timeout=180)
                    exports.append({'path': xml.name, 'sha256': digest(xml),
                                    **native_threads(xml, executable.name, backend)})
            require(exports, 'No supported Time Profiler stack table; available schemas: ' + str(schemas))
            results.append({'corpus': document['id'], 'backend': backend, 'inputSha256': document['sha256'],
                            'executableSha256': executable_sha, 'reportSha256': digest(config['output']),
                            'exports': exports, 'textCharacters': [r['textCharacters'] for r in report['observations']]})
            write_json(traces / 'summary.json', {'traceRuns': results, 'complete': False})
            require(sum(e['threadSamples'] for e in exports) > 0, 'No resolved thread/stack samples were exported')
    require(digest(executable) == executable_sha, 'Executable changed between traces')
    summary = {'complete': True, 'sourceCommit': source_commit, 'baselineProvenanceSha256': provenance_sha256,
               'executableSha256': executable_sha, 'xctraceVersion': version.strip(), 'traceRuns': results,
               'nativeThreadAttributionAvailable': all(sum(e['nativeSymbolSamples'] for e in r['exports']) > 0
                                                       for r in results),
               'baselineTimingsProfiled': False, 'timingsComparableWithBaseline': False,
               'physicalDevice': False, 'appUiEquivalenceProven': False,
               'processAttribution': 'Executable name matched in exported stacks; PIDs are retained, '
                                     'but there is no independent launch-PID receipt.',
               'scope': 'Sampled native stacks during the same binary real-PDF text/render workload; '
                        'absence of native symbols is missing attribution, not evidence of no native work.'}
    write_json(traces / 'summary.json', summary)
    return summary

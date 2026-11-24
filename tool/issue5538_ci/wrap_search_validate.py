"""Validate wrap-search evidence without treating instrumentation as product timing."""
from collections import defaultdict
import statistics

ORDERS = ('page1', 'start75', 'redirect')
QUERIES = ('app-simple', 'app-user-regex')
FIXED_CANDIDATES = ('בראשית', 'אלהים', 'ישראל', 'משה', 'ויאמר', 'אמר', 'הוא', 'את')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_queries(queries):
    require([row['id'] for row in queries] == list(QUERIES), 'Missing application query path')
    simple, regex = queries
    require(simple['builder'] == 'PdfBookSearchView.buildSimpleSearchPattern'
            and regex['builder'] == 'PdfUserRegexPattern.tryParse', 'Pattern builders changed')
    require(simple['query'] in FIXED_CANDIDATES and regex['query'] == '/' + simple['query'] + '/'
            and regex.get('planKind') == 'singleWord'
            and regex.get('runtimeType') == 'PdfUserRegexPattern',
            'User regex is not the fixed candidate accepted by the actual product parser')
    selection = simple['selection']
    require(regex['selection'] == selection and selection['candidateOrder'] == list(FIXED_CANDIDATES)
            and selection['wholeWord'] is False, 'Query selection contract differs')
    checks = selection['presenceChecks']
    selected = FIXED_CANDIDATES.index(simple['query'])
    require([row['query'] for row in checks] == list(FIXED_CANDIDATES[:selected + 1]),
            'Query selection skipped or reordered a fixed candidate')
    keys = ('simpleBefore', 'simpleAfter', 'regexBefore', 'regexAfter')
    require(all(type(row.get(key)) is bool for row in checks for key in keys)
            and all(checks[-1][key] for key in keys)
            and not any(all(row[key] for key in keys) for row in checks[:-1]),
            'Selected queries are not the first positive pair across the wrap boundary')


def expected_order(pages, target, order):
    if order == 'page1':
        return list(range(1, pages + 1))
    if order == 'start75':
        return list(range(target, pages + 1)) + list(range(1, target))
    if order == 'redirect':
        return [1, 2] + list(range(target, pages + 1)) + list(range(3, target))
    raise ValueError('Unknown scan order')


def validate_probes(row, pages):
    ranges = row['pageRanges']
    counts = {item['pageNumber']: item['end'] - item['start'] for item in ranges}
    probes = row['insertionProbes']
    require(len(probes) == pages, 'Missing insertion observations')
    seen, length = {}, 0
    for page, probe in zip(row['scanOrder'], probes):
        added = counts[page]
        offset = sum(value for previous, value in seen.items() if previous < page)
        copied = added > 0 and offset != length
        expected = {'pageNumber': page, 'beforeLength': length, 'addedMatches': added,
                    'insertAt': offset, 'afterLength': length + added,
                    'copiedExistingReferences': length if copied else 0,
                    'newListReferences': length + added if copied else 0,
                    'allocatedNewResultList': copied,
                    'currentIndexBefore': None, 'currentIndexAfter': None}
        require(all(probe.get(key) == value for key, value in expected.items()), 'Insertion count/offset proof differs from page ranges')
        require(type(probe.get('instrumentedInsertionUs')) is int and probe['instrumentedInsertionUs'] >= 0,
                'Invalid diagnostic insertion time')
        seen[page] = added
        length += added
    return {'wrapInsertions': sum(row['allocatedNewResultList'] for row in probes),
            'copiedExistingReferences': sum(row['copiedExistingReferences'] for row in probes),
            'newResultListReferences': sum(row['newListReferences'] for row in probes),
            'instrumentedInsertionUs': sum(row['instrumentedInsertionUs'] for row in probes),
            'instrumentedSessionUs': row['sessionElapsedUs'],
            'allocationBytesMeasured': False, 'instrumentedTimesAreProductTimes': False}


def validate_report(report):
    require(report.get('schemaVersion') == 1 and report.get('status') == 'passed', 'Incomplete report')
    require(report.get('executionMode') == 'flutter-profile'
            and report.get('component') == 'actual-centered-search-session-wrap-insertion', 'Wrong experiment')
    variant, order = report['variant'], report['scanCondition']
    require(variant in ('control', 'counts') and order in ORDERS, 'Invalid condition')
    pages, target = report['pageCount'], report['targetPage']
    require(type(pages) is int and pages > 3 and type(target) is int and target == pages * 3 // 4,
            'Changed wrap boundary')
    require(report['warmup'] == 1 and report['repetitions'] == (5 if variant == 'control' else 1),
            'Unexpected dependent pass count')
    validate_queries(report['queries'])
    expected_keys = {(query, warmup, iteration) for query in QUERIES
                     for warmup, iteration in [(True, 0), *[(False, x) for x in range(report['repetitions'])]]}
    rows = report['observations']
    actual_keys = [(row.get('queryId'), row.get('warmup'), row.get('iteration')) for row in rows]
    require(len(actual_keys) == len(set(actual_keys)) and set(actual_keys) == expected_keys, 'Duplicate or missing observations')
    signatures = {}
    for row in [*rows, *report['correctness']]:
        require(row['scanOrder'] == expected_order(pages, target, order), 'Page scan order differs')
        require(row['redirectCount'] == (1 if order == 'redirect' else 0), 'Incorrect redirect count')
        require(type(row['resultCount']) is int and row['resultCount'] > 0, 'Empty workload')
        ranges = row['pageRanges']
        require([item['pageNumber'] for item in ranges] == list(range(1, pages + 1)), 'Missing page range')
        offset = 0
        for item in ranges:
            require(type(item['start']) is int and type(item['end']) is int
                    and item['start'] == offset and item['end'] >= offset, 'Noncontiguous page result ranges')
            offset = item['end']
        require(offset == row['resultCount'], 'Page ranges disagree with result total')
        key = row['queryId']
        signature = (row['resultSha256'], row['resultCount'], ranges)
        require(key not in signatures or signatures[key] == signature, 'Result/geometry/order parity changed between passes')
        signatures[key] = signature
        if row['correctnessOnly']:
            require(type(row['currentIndex']) is int and 0 <= row['currentIndex'] < row['resultCount']
                    and row.get('selectedKey'), 'Current-index selection was not exercised')
            require(row['retainedSnapshots'] and all(item['sha256Before'] == item['sha256After']
                    and item['length'] > 0 for item in row['retainedSnapshots']), 'Retained published snapshot changed')
        else:
            require(row['currentIndex'] is None and row['selectedKey'] is None, 'Timing pass changed selection')
            require(type(row['sessionElapsedUs']) is int and row['sessionElapsedUs'] > 0
                    and type(row['firstTargetMatchNotificationUs']) is int
                    and 0 <= row['firstTargetMatchNotificationUs'] <= row['sessionElapsedUs'], 'Invalid session timing')
            require(row['firstTargetMatchPage'] >= target and row['firstTargetMatchPage'] <= pages, 'Invalid target match page')
            if variant == 'counts':
                require(row['timingClass'] == 'instrumented-diagnostic', 'Instrumented row mislabeled as product timing')
                totals = validate_probes(row, pages)
                require(totals['wrapInsertions'] == 0 if order == 'page1' else totals['wrapInsertions'] > 0,
                        'Wrap path was not exercised as declared')
            else:
                require(row['timingClass'] == 'unmodified-control' and row['insertionProbes'] == [],
                        'Control contains instrumented samples')
    require([row['queryId'] for row in report['correctness']] == list(QUERIES)
            and all(row['correctnessOnly'] is True for row in report['correctness']), 'Missing correctness pass')
    return report


def summarize(results, *, complete):
    groups = defaultdict(list)
    seen = set()
    for result in results:
        report = validate_report(result['report'])
        key = (report['inputSha256'], report['variant'], report['scanCondition'], result['block'])
        require(key not in seen, 'Repeated independent process identity')
        seen.add(key)
        groups[report['inputSha256']].append(result)
    if complete:
        require(len(groups) == 2 and len(results) == 24, 'Incomplete two-corpus process matrix')
    documents = []
    invariant = ('sourceCommit', 'host', 'dartVersion', 'inputSha256', 'pageCount', 'inputBytes',
                 'pdfiumSha256', 'searchEngineSha256', 'queryDescriptorsSha256', 'pageTextHashesSha256', 'referenceSha256')
    for input_sha, values in groups.items():
        first = values[0]['report']
        for value in values[1:]:
            require(all(value['report'][key] == first[key] for key in invariant),
                    'Source/native/corpus/query/reference identity differs between processes')
            require([(x['queryId'], x['resultSha256'], x['resultCount'], x['pageRanges']) for x in value['report']['correctness']]
                    == [(x['queryId'], x['resultSha256'], x['resultCount'], x['pageRanges']) for x in first['correctness']],
                    'Cross-process result parity differs')
        if complete:
            expected = {('control', order, block) for order in ORDERS for block in range(3)} | {('counts', order, None) for order in ORDERS}
            require({(v['report']['variant'], v['report']['scanCondition'], v['block']) for v in values} == expected,
                    'Incomplete per-corpus matrix')
        timings, diagnostics = [], []
        for query in QUERIES:
            process_rows = []
            for value in values:
                report = value['report']
                measured = [row for row in report['observations'] if row['queryId'] == query and not row['warmup']]
                if report['variant'] == 'control':
                    process_rows.append({'block': value['block'], 'condition': report['scanCondition'], 'path': value['path'],
                      'dependentPassesUs': [r['sessionElapsedUs'] for r in measured],
                      'sessionProcessMedianUs': statistics.median(r['sessionElapsedUs'] for r in measured),
                      'firstTargetNotificationProcessMedianUs': statistics.median(r['firstTargetMatchNotificationUs'] for r in measured)})
                else:
                    diagnostics.append({'queryId': query, 'condition': report['scanCondition'], 'path': value['path'],
                                        **validate_probes(measured[0], report['pageCount'])})
            conditions = []
            for order in ORDERS:
                members = [p for p in process_rows if p['condition'] == order]
                if not members:
                    continue
                medians = [p['sessionProcessMedianUs'] for p in members]
                changes = []
                for member in members:
                    control = next((p for p in process_rows if p['condition'] == 'page1' and p['block'] == member['block']), None)
                    if control:
                        changes.append({'block': member['block'], 'sessionChangePercentVsPage1':
                          (member['sessionProcessMedianUs'] / control['sessionProcessMedianUs'] - 1) * 100})
                conditions.append({'condition': order, 'independentProcesses': len(members),
                    'sessionMedianOfProcessMediansUs': statistics.median(medians),
                    'sessionProcessMedianRangeUs': [min(medians), max(medians)], 'pairedBlocks': changes})
            timings.append({'queryId': query, 'unmodifiedControlProcesses': process_rows, 'conditions': conditions})
        documents.append({'inputSha256': input_sha, 'pageCount': first['pageCount'], 'queries': first['queries'],
                          'controlTimings': timings, 'instrumentedDiagnosticsOnly': diagnostics})
    return {'schemaVersion': 1, 'status': 'passed' if complete else 'partial', 'declaredScopeComplete': complete,
            'completedIndependentProcesses': len(results), 'buildsPlanned': 2,
            'documents': documents, 'controlConditionOrders': [list(x) for x in (ORDERS, tuple(reversed(ORDERS)), ('start75', 'page1', 'redirect'))],
            'limitations': ['No implementation/backport comparison; source algorithm is locked pdfrx 2.13.1.',
              'Three independent control processes per condition; five dependent passes contribute one process median.',
              'Per-block ratios compare scan orders on one host and are descriptive; no significance or population-speedup claim.',
              'Instrumentation counts logical copied references, not native/Dart allocation bytes or heap peak.',
              'Instrumented durations cannot be subtracted from or compared as product timing against unmodified controls.',
              'Native extraction, query construction, viewer rendering/navigation and app input latency are outside this component timing.']}

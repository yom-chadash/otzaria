import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:otzaria/pdf_book/utils/pdf_book_layout.dart';
import 'package:otzaria/pdf_book/utils/pdf_page_highlights.dart';
import 'package:otzaria/pdf_book/utils/result_lines_highlight_pattern.dart';
import 'package:otzaria/search/search_engine_library.dart';
import 'package:pdfrx/pdfrx.dart';

import 'support/pdf_proximity_profile_queries.dart';

const _pdfiumSha256 =
    '1c251efce460d67437a4c76d4be9033ae2f17bf5603f5bff1af10209b94ca723';
const _markSource = r'[\u0591-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]';
const _warmup = int.fromEnvironment(
  'PDF_HIGHLIGHT_PROFILE_WARMUP',
  defaultValue: 1,
);
const _repetitions = int.fromEnvironment(
  'PDF_HIGHLIGHT_PROFILE_REPETITIONS',
  defaultValue: 7,
);

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  testWidgets('actual app highlight and layout profile', (tester) async {
    expect(
      kProfileMode,
      isTrue,
      reason: 'This measurement requires profile mode',
    );
    await tester.runAsync(() async {
      final output = Directory(
        Platform.environment['PDF_HIGHLIGHT_PROFILE_OUTPUT'] ??
            const String.fromEnvironment('PDF_HIGHLIGHT_PROFILE_OUTPUT'),
      );
      expect(output.path, isNotEmpty);
      expect(await output.exists(), isFalse);
      await output.create(recursive: true);
      try {
        final report = await _run(output);
        await _writeJson(File('${output.path}/report.json'), report);
        binding.reportData = {
          'status': 'passed',
          'report': '${output.path}/report.json',
        };
      } catch (error, stack) {
        await _writeJson(File('${output.path}/failure.json'), {
          'status': 'failed',
          'error': error.toString(),
          'stack': stack.toString(),
        });
        rethrow;
      }
    });
  }, timeout: const Timeout(Duration(minutes: 20)));
}

Future<Map<String, Object?>> _run(Directory output) async {
  final querySuite =
      Platform.environment['PDF_HIGHLIGHT_PROFILE_QUERY_SUITE'] ?? 'legacy-v1';
  expect(querySuite, isIn(['legacy-v1', pdfProximityProfileSuite]));
  final proximity = querySuite == pdfProximityProfileSuite;
  final inputSha256 =
      Platform.environment['PDF_HIGHLIGHT_PROFILE_SHA256'] ??
      const String.fromEnvironment('PDF_HIGHLIGHT_PROFILE_SHA256');
  const sourceCommit = String.fromEnvironment(
    'PDF_HIGHLIGHT_PROFILE_SOURCE_COMMIT',
  );
  const highlightSha256 = String.fromEnvironment(
    'PDF_HIGHLIGHT_PROFILE_SOURCE_SHA256',
  );
  final input = File(
    Platform.environment['PDF_HIGHLIGHT_PROFILE_INPUT'] ??
        const String.fromEnvironment('PDF_HIGHLIGHT_PROFILE_INPUT'),
  );
  final pdfium = File(
    Platform.environment['PDF_HIGHLIGHT_PROFILE_PDFIUM'] ??
        const String.fromEnvironment('PDF_HIGHLIGHT_PROFILE_PDFIUM'),
  );
  expect(_warmup, greaterThanOrEqualTo(0));
  expect(_repetitions, greaterThanOrEqualTo(1));
  expect(RegExp(r'^[0-9a-f]{40}$').hasMatch(sourceCommit), isTrue);
  expect(RegExp(r'^[0-9a-f]{64}$').hasMatch(highlightSha256), isTrue);
  expect(await _fileHash(input), inputSha256);
  expect(await _fileHash(pdfium), _pdfiumSha256);
  Pdfrx.pdfiumModulePath = await pdfium.resolveSymbolicLinks();
  Pdfrx.normalizeHebrewText = true;
  Pdfrx.decodeLegacyHebrewText = true;
  final cache = await Directory('${output.path}/native-cache').create();
  await pdfrxInitialize(tmpPath: cache.path);
  final document = await PdfDocument.openFile(input.path);
  final stream = File(
    '${output.path}/observations.jsonl',
  ).openSync(mode: FileMode.writeOnly);
  try {
    final texts = <PdfPageText>[];
    final sizes = <Size>[];
    for (final page in document.pages) {
      texts.add(await page.loadStructuredText());
      sizes.add(Size(page.width, page.height));
    }
    expect(texts, isNotEmpty);
    Map<String, Object?>? searchEngine;
    if (proximity) {
      await SearchEngineLibrary.ensureLoaded();
      searchEngine = await _searchEngineIdentity();
    }
    final selected = proximity ? buildPdfProximityProfileQueries(texts) : null;
    final queries = selected?.queries ?? <PdfHighlightProfileQuery>[
      (
        id: 'literal_alef',
        pattern: 'א' as Pattern,
        descriptor: <String, Object>{
          'kind': 'literal',
          'source': 'א',
          'caseInsensitive': true,
        },
      ),
      (
        id: 'hebrew_combining_marks',
        pattern: RegExp(_markSource) as Pattern,
        descriptor: <String, Object>{'kind': 'regex', 'source': _markSource},
      ),
      (
        id: 'result_lines_alef_bet',
        pattern:
            ResultLinesHighlightPattern.build(
                  ['א', 'ב'],
                  [
                    {0, 1},
                  ],
                )!
                as Pattern,
        descriptor: <String, Object>{
          'kind': 'ResultLinesHighlightPattern',
          'terms': ['א', 'ב'],
          'termSets': [
            [0, 1],
          ],
        },
      ),
    ];
    final references = <String, List<String>>{};
    final referenceCounts = <String, int>{};
    final referenceSpans = <Map<String, Object?>>[];
    for (final query in queries) {
      final signatures = <String>[];
      var count = 0;
      for (final text in texts) {
        final results = await _reference(text, query.pattern);
        signatures.add(_signature(results));
        count += results.length;
        if (proximity) {
          final spans = [for (final result in results) [result.start, result.end]];
          referenceSpans.add({
            'queryId': query.id,
            'pageNumber': text.pageNumber,
            'spans': spans,
            'spansSha256': sha256
                .convert(utf8.encode(jsonEncode(spans)))
                .toString(),
            'resultsSha256': _signature(results),
          });
        }
      }
      expect(count, greaterThan(0), reason: 'Empty workload: ${query.id}');
      references[query.id] = signatures;
      referenceCounts[query.id] = count;
    }
    final rows = <Map<String, Object?>>[];
    for (var iteration = 0; iteration < _warmup + _repetitions; iteration++) {
      for (final query in queries) {
        var loads = 0;
        var notifications = 0;
        final highlights = PdfPageHighlights(
          loadText: (page) {
            loads++;
            return Future.value(texts[page - 1]);
          },
          onChanged: () => notifications++,
        )..setPattern(query.pattern);
        try {
          for (final text in texts) {
            final clock = Stopwatch()..start();
            final result = await highlights.ensurePage(text.pageNumber);
            clock.stop();
            expect(result, isNotNull);
            final signature = _signature(result!);
            expect(signature, references[query.id]![text.pageNumber - 1]);
            final row = <String, Object?>{
              'iteration': iteration,
              'warmup': iteration < _warmup,
              'queryId': query.id,
              'pageNumber': text.pageNumber,
              'elapsedUs': clock.elapsedMicroseconds,
              'matches': result.length,
              'rects': result.fold<int>(
                0,
                (n, match) => n + match.rects.length,
              ),
              'resultsSha256': signature,
              'parity': true,
            };
            rows.add(row);
            stream.writeStringSync('${jsonEncode(row)}\n');
          }
          expect(loads, texts.length);
          expect(notifications, texts.length);
          expect(highlights.cachedPageCount, lessThanOrEqualTo(8));
        } finally {
          highlights.dispose();
        }
      }
    }
    final microtasks = <Map<String, Object?>>[];
    for (final query in queries) {
      final highlights = PdfPageHighlights(
        loadText: (page) => Future.value(texts[page - 1]),
        onChanged: () {},
      )..setPattern(query.pattern);
      try {
        for (final text in texts) {
          var count = 0;
          final result = await runZoned(
            () => highlights.ensurePage(text.pageNumber),
            zoneSpecification: ZoneSpecification(
              scheduleMicrotask: (self, parent, zone, action) {
                count++;
                parent.scheduleMicrotask(zone, action);
              },
            ),
          );
          expect(
            _signature(result!),
            references[query.id]![text.pageNumber - 1],
          );
          microtasks.add({
            'queryId': query.id,
            'pageNumber': text.pageNumber,
            'scheduledMicrotasks': count,
            'matches': result.length,
            'resultsSha256': _signature(result),
            'parity': true,
          });
        }
      } finally {
        highlights.dispose();
      }
    }
    final layout = _layoutProfile(sizes);
    expect(await _fileHash(input), inputSha256);
    expect(await _fileHash(pdfium), _pdfiumSha256);
    if (proximity) {
      expect(await _searchEngineIdentity(), searchEngine);
    }
    final descriptors = [
      for (final query in queries) {'id': query.id, ...query.descriptor},
    ];
    return {
      'protocolVersion': 1,
      'status': 'passed',
      'executionMode': 'flutter-profile',
      'component': 'otzaria-page-highlights-and-book-layout',
      'sourceCommit': sourceCommit,
      'highlightSourceSha256': highlightSha256,
      'dartVersion': Platform.version,
      'host': {
        'name': Platform.localHostname,
        'operatingSystem': Platform.operatingSystem,
        'operatingSystemVersion': Platform.operatingSystemVersion,
        'processors': Platform.numberOfProcessors,
      },
      'inputSha256': inputSha256,
      'inputBytes': await input.length(),
      'pdfiumSha256': _pdfiumSha256,
      'sourcePageCount': texts.length,
      'warmup': _warmup,
      'repetitions': _repetitions,
      'contextLength': 80,
      'maxCachedPages': 8,
      'normalizeHebrewText': true,
      'decodeLegacyHebrewText': true,
      'highlightScope':
          'actual app ensurePage with preloaded real structured text; extraction excluded',
      'uiLatencyCollected': false,
      'microtaskAuditTimed': false,
      'querySuite': querySuite,
      'queries': descriptors,
      if (proximity) ...{
        'processId': pid,
        'querySelection': selected!.selection,
        'queryDescriptorsSha256': sha256
            .convert(utf8.encode(jsonEncode(descriptors)))
            .toString(),
        'searchEngine': searchEngine,
        'referenceSpans': referenceSpans,
        'measurementTarget':
            'actual product advanced-search highlight helper; '
            'derived native-text snippets; search retrieval and UI excluded',
      },
      'referenceMatchesPerQuery': referenceCounts,
      'textInventory': [
        for (final text in texts)
          {
            'pageNumber': text.pageNumber,
            'textCharacters': text.fullText.length,
            'textSha256': sha256.convert(utf8.encode(text.fullText)).toString(),
            'characterRects': text.charRects.length,
            'fragments': text.fragments.length,
          },
      ],
      'observations': rows,
      'microtaskAudit': microtasks,
      'layout': layout,
    };
  } finally {
    stream.closeSync();
    await document.dispose();
    await PdfrxEntryFunctions.instance.stopBackgroundWorker();
  }
}

Future<Map<String, Object?>> _searchEngineIdentity() async {
  final executable = File(Platform.resolvedExecutable);
  final library = File('${executable.parent.path}/lib/libsearch_engine.so');
  final resolved = await library.resolveSymbolicLinks();
  final mappings = await File('/proc/self/maps').readAsLines();
  final mapped = mappings.any(
    (line) => line.trimRight().endsWith(' $resolved'),
  );
  expect(mapped, isTrue, reason: 'The identified search engine must be loaded');
  return {
    'initializer': 'SearchEngineLibrary.ensureLoaded',
    'packageVersion': '7.2.1',
    'libraryPath': resolved,
    'librarySha256': await _fileHash(library),
    'libraryBytes': await library.length(),
    'mappedIntoProcess': mapped,
    'initializationTimed': false,
  };
}

Future<List<PdfTextSearchResult>> _reference(
  PdfPageText text,
  Pattern pattern,
) async {
  if (pattern is String || pattern is RegExp) {
    return text
        .allMatches(pattern, caseInsensitive: true)
        .map((range) => PdfTextSearchResult.fromRange(range))
        .toList();
  }
  return [
    for (final match in pattern.allMatches(text.fullText))
      if (match.end > match.start)
        PdfTextSearchResult.fromRange(
          PdfPageTextRange(pageText: text, start: match.start, end: match.end),
        ),
  ];
}

String _signature(List<PdfTextSearchResult> results) => sha256
    .convert(
      utf8.encode(
        jsonEncode([
          for (final result in results)
            [
              result.pageNumber,
              result.start,
              result.end,
              result.contextStart,
              result.contextText,
              result.pageTextLength,
              [
                for (final rect in result.rects)
                  [rect.left, rect.top, rect.right, rect.bottom],
              ],
            ],
        ]),
      ),
    )
    .toString();

List<Map<String, Object?>> _layoutProfile(List<Size> realSizes) {
  final rows = <Map<String, Object?>>[];
  final inputs = [
    (kind: 'real-document-metadata', sizes: realSizes),
    for (final count in [100, 1000, 10000])
      (
        kind: 'derived-real-metadata-repeated',
        sizes: List.generate(count, (i) => realSizes[i % realSizes.length]),
      ),
    for (final count in [100, 1000, 10000])
      (
        kind: 'synthetic-mixed-sizes',
        sizes: List.generate(count, (i) => Size(400.0 + i % 3, 600.0 + i % 5)),
      ),
  ];
  for (final input in inputs) {
    for (var iteration = 0; iteration < _warmup + _repetitions; iteration++) {
      for (final scenario in [
        'cold',
        'cache_hit',
        'params_change',
        'dimensions_change',
      ]) {
        final pages = [for (final size in input.sizes) _CountingPage(size)];
        final memo = BookViewLayoutMemo();
        final prior = scenario == 'cold'
            ? null
            : memo.layout(pages, hasCover: true, verticalMargin: 16);
        if (scenario == 'dimensions_change') {
          pages.last.value += const Offset(0, 1);
        }
        for (final page in pages) {
          page.widthReads = 0;
          page.heightReads = 0;
        }
        final hasCover = scenario != 'params_change';
        final margin = scenario == 'params_change' ? 8.0 : 16.0;
        final clock = Stopwatch()..start();
        final result = memo.layout(
          pages,
          hasCover: hasCover,
          verticalMargin: margin,
        );
        clock.stop();
        final reads = [
          pages.fold<int>(0, (n, p) => n + p.widthReads),
          pages.fold<int>(0, (n, p) => n + p.heightReads),
        ];
        final reference = buildBookViewPageLayout(
          pageSizes: [for (final p in pages) p.value],
          hasCover: hasCover,
          verticalMargin: margin,
        );
        expect(result.pageLayouts, reference.pageLayouts);
        expect(result.documentSize, reference.documentSize);
        expect(identical(prior, result), scenario == 'cache_hit');
        rows.add({
          'inputKind': input.kind,
          'pageCount': pages.length,
          'sourceDocumentPageCount': input.kind == 'synthetic-mixed-sizes'
              ? null
              : realSizes.length,
          'iteration': iteration,
          'warmup': iteration < _warmup,
          'scenario': scenario,
          'elapsedUs': clock.elapsedMicroseconds,
          'widthReads': reads[0],
          'heightReads': reads[1],
          'sameLayoutInstance': identical(prior, result),
          'parity': true,
          'layoutSha256': sha256
              .convert(
                utf8.encode(
                  jsonEncode([
                    result.documentSize.width,
                    result.documentSize.height,
                    [
                      for (final rect in result.pageLayouts)
                        [rect.left, rect.top, rect.right, rect.bottom],
                    ],
                  ]),
                ),
              )
              .toString(),
        });
      }
    }
  }
  return rows;
}

class _CountingPage extends Fake implements PdfPage {
  _CountingPage(this.value);
  Size value;
  int widthReads = 0;
  int heightReads = 0;

  @override
  double get width {
    widthReads++;
    return value.width;
  }

  @override
  double get height {
    heightReads++;
    return value.height;
  }
}

Future<String> _fileHash(File file) async =>
    (await sha256.bind(file.openRead()).single).toString();

Future<void> _writeJson(File file, Object value) async {
  await file.writeAsString(
    '${const JsonEncoder.withIndent('  ').convert(value)}\n',
    flush: true,
  );
}

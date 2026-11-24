// Fork-only component experiment. No viewer/UI latency or allocation claims.
// ignore_for_file: invalid_use_of_visible_for_testing_member
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:otzaria/pdf_book/utils/centered_text_search_session.dart';
import 'package:otzaria/pdf_book/utils/pdf_user_regex_pattern.dart';
import 'package:otzaria/pdf_book/view/pdf_search_screen.dart';
import 'package:otzaria/search/search_engine_library.dart';
import 'package:pdfrx/pdfrx.dart';

import 'wrap_search_metrics.dart';

const _instrumented = bool.fromEnvironment('WRAP_SEARCH_INSTRUMENTED');
const _source = String.fromEnvironment('WRAP_SEARCH_SOURCE');
const _pdfiumSha =
    '1c251efce460d67437a4c76d4be9033ae2f17bf5603f5bff1af10209b94ca723';
const _candidates = [
  'בראשית',
  'אלהים',
  'ישראל',
  'משה',
  'ויאמר',
  'אמר',
  'הוא',
  'את',
];

void _require(bool condition, String message) {
  if (!condition) throw StateError(message);
}

String _hash(Object? value) =>
    sha256.convert(utf8.encode(jsonEncode(value))).toString();
Future<String> _fileHash(File file) async =>
    (await sha256.bind(file.openRead()).first).toString();

Future<void> _json(File file, Object? value) async {
  _require(!await file.exists(), 'Output already exists: ${file.path}');
  final temporary = File('${file.path}.tmp');
  await temporary.writeAsString('${jsonEncode(value)}\n', flush: true);
  await temporary.rename(file.path);
}

Future<void> _waitFor(File file) async {
  final deadline = DateTime.now().add(const Duration(seconds: 90));
  while (!await file.exists()) {
    _require(
      DateTime.now().isBefore(deadline),
      'Host handshake timed out: ${file.path}',
    );
    await Future<void>.delayed(const Duration(milliseconds: 50));
  }
}

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const MaterialApp(home: SizedBox.shrink()));
  unawaited(_main());
}

Future<void> _main() async {
  final output = Directory(Platform.environment['WRAP_SEARCH_OUTPUT']!);
  var result = 1;
  try {
    _require(
      kProfileMode && Platform.isLinux,
      'Requires Linux Flutter profile',
    );
    _require(
      RegExp(r'^[a-f0-9]{40}$').hasMatch(_source),
      'Missing compiled source identity',
    );
    _require(!await output.exists(), 'Output directory must be new');
    await output.create(recursive: true);
    final report = await _run(output);
    final reportFile = File('${output.path}/report.json');
    await _json(reportFile, report);
    await _json(File('${output.path}/completion.json'), {
      'schemaVersion': 1,
      'status': 'passed',
      'processId': pid,
      'sourceCommit': _source,
      'variant': _instrumented ? 'counts' : 'control',
      'reportSha256': await _fileHash(reportFile),
      'measurementsFinished': true,
    });
    await _waitFor(File('${output.path}/stop'));
    result = 0;
  } catch (error, stack) {
    if (await output.exists()) {
      await _json(File('${output.path}/failure.json'), {
        'status': 'failed',
        'error': '$error',
        'stack': '$stack',
        'sourceCommit': _source,
        'processId': pid,
      });
    }
    stderr.writeln('$error\n$stack');
  }
  exit(result);
}

// The real search session and patterns run unchanged. This document-only
// controller supplies native document ownership and records selection, without
// mounting a PdfViewer or claiming navigation/paint/frame timings.
class _DocumentController extends PdfViewerController {
  _DocumentController(this.document);
  @override
  final PdfDocument document;
  int? selectedPage;
  @override
  bool get isReady => true;
  @override
  FutureOr<T?> useDocument<T>(
    FutureOr<T> Function(PdfDocument document) task, {
    bool ensureLoaded = true,
    Completer<dynamic>? cancelLoading,
  }) => task(document);
  @override
  Rect calcRectForRectInsidePage({
    required int pageNumber,
    required PdfRect rect,
  }) {
    final height = document.pages[pageNumber - 1].height;
    return Rect.fromLTRB(
      rect.left,
      height - rect.top,
      rect.right,
      height - rect.bottom,
    );
  }

  @override
  Matrix4 calcMatrixFor(Offset position, {double? zoom, Size? viewSize}) =>
      Matrix4.identity();
  @override
  Future<void> goTo(
    Matrix4? destination, {
    Duration duration = const Duration(milliseconds: 200),
  }) async {}
  @override
  void setCurrentPageNumber(int pageNumber) => selectedPage = pageNumber;
  @override
  void invalidate() {}
}

class _Session extends CenteredTextSearchSession {
  _Session(super.controller, this.texts);
  final List<PdfPageText> texts;
  final List<int> scanOrder = [];
  void Function(int)? beforeLoad;
  @override
  Future<PdfPageText?> loadText({required int pageNumber}) async {
    beforeLoad?.call(pageNumber);
    scanOrder.add(pageNumber);
    return texts[pageNumber - 1];
  }
}

Map<String, Object?> _result(PdfTextSearchResult match) => {
  'pageNumber': match.pageNumber,
  'start': match.start,
  'end': match.end,
  'contextStart': match.contextStart,
  'contextText': match.contextText,
  'pageTextLength': match.pageTextLength,
  'rects': [
    for (final rect in match.rects)
      [rect.left, rect.top, rect.right, rect.bottom],
  ],
};
String _key(PdfTextSearchResult match) =>
    '${match.pageNumber}:${match.start}:${match.end}';
String _signature(Iterable<PdfTextSearchResult> matches) =>
    _hash(matches.map(_result).toList());

typedef _Query = ({
  String id,
  Pattern pattern,
  Map<String, Object?> descriptor,
});

List<_Query> _queries(List<PdfPageText> texts, int target) {
  final presence = <Map<String, Object?>>[];
  for (final word in _candidates) {
    final literal = PdfBookSearchView.buildSimpleSearchPattern(
      word,
      wholeWord: false,
    );
    _require(
      literal != null,
      'Application simple-pattern builder rejected candidate',
    );
    final userInput = '/${literal!.source}/';
    final regex = PdfUserRegexPattern.tryParse(userInput);
    _require(regex != null, 'Application regex parser rejected candidate');
    final simpleBefore = texts
        .take(target - 1)
        .any((t) => literal.regExp.hasMatch(t.fullText));
    final simpleAfter = texts
        .skip(target - 1)
        .any((t) => literal.regExp.hasMatch(t.fullText));
    final regexBefore = texts
        .take(target - 1)
        .any((t) => regex!.hasMatch(t.fullText));
    final regexAfter = texts
        .skip(target - 1)
        .any((t) => regex!.hasMatch(t.fullText));
    presence.add({
      'query': word,
      'simpleBefore': simpleBefore,
      'simpleAfter': simpleAfter,
      'regexBefore': regexBefore,
      'regexAfter': regexAfter,
    });
    if (simpleBefore && simpleAfter && regexBefore && regexAfter) {
      final selection = {
        'algorithm': 'first fixed candidate with positive matches before and at/after 75%; no timings consulted',
        'candidateOrder': _candidates,
        'presenceChecks': presence,
        'wholeWord': false,
      };
      return [
        (
          id: 'app-simple',
          pattern: literal.regExp,
          descriptor: {
            'query': word,
            'source': literal.source,
            'builder': 'PdfBookSearchView.buildSimpleSearchPattern',
            'runtimeType': '${literal.regExp.runtimeType}',
            'selection': selection,
          },
        ),
        (
          id: 'app-user-regex',
          pattern: regex!,
          descriptor: {
            'query': userInput,
            'source': regex.pattern,
            'builder': 'PdfUserRegexPattern.tryParse',
            'runtimeType': '${regex.runtimeType}',
            'selection': selection,
          },
        ),
      ];
    }
  }
  throw StateError(
    'No fixed candidate is positive on both sides of the wrap boundary; no empty workload is permitted. $presence',
  );
}

Future<List<PdfTextSearchResult>> _reference(
  List<PdfPageText> texts,
  Pattern pattern,
) async {
  final result = <PdfTextSearchResult>[];
  for (final text in texts) {
    await for (final range in text.allMatches(pattern, caseInsensitive: true)) {
      result.add(PdfTextSearchResult.fromRange(range, contextLength: 80));
    }
  }
  return result;
}

List<int> _expectedOrder(int pages, int target, String order) =>
    switch (order) {
      'page1' => [for (var page = 1; page <= pages; page++) page],
      'start75' => [
        for (var page = target; page <= pages; page++) page,
        for (var page = 1; page < target; page++) page,
      ],
      'redirect' => [
        1,
        2,
        for (var page = target; page <= pages; page++) page,
        for (var page = 3; page < target; page++) page,
      ],
      _ => throw StateError('Unknown scan order: $order'),
    };

Future<Map<String, Object?>> _scan(
  PdfDocument document,
  List<PdfPageText> texts,
  _Query query,
  List<PdfTextSearchResult> reference,
  String order,
  int target, {
  required bool correctness,
}) async {
  final controller = _DocumentController(document);
  final session = _Session(controller, texts);
  final done = Completer<void>();
  final clock = Stopwatch();
  var redirected = false;
  int? firstTargetNotification;
  final targetMatchPage = reference
      .firstWhere((m) => m.pageNumber >= target)
      .pageNumber;
  final retained =
      <
        ({
          List<PdfTextSearchResult> list,
          String hash,
          int length,
          int afterPage,
        })
      >[];
  String? selected;
  final pendingSelections = <Future<int>>[];
  void capture(int afterPage) {
    if (!correctness || session.matches.isEmpty || retained.length >= 3) return;
    retained.add((
      list: session.matches,
      hash: _signature(session.matches),
      length: session.matches.length,
      afterPage: afterPage,
    ));
  }

  session.beforeLoad = (page) {
    if (correctness && session.scanOrder.isNotEmpty) {
      final previous = session.scanOrder.last;
      if (retained.isEmpty ||
          page < previous ||
          (retained.length == 2 && previous < target)) {
        capture(previous);
      }
      if (selected == null && session.matches.isNotEmpty) {
        selected = _key(session.matches.first);
        pendingSelections.add(session.goToMatchOfIndex(0));
      }
    }
    if (order == 'redirect' && session.scanOrder.length == 1) {
      _require(
        page == 2 && !redirected && session.isSearching,
        'Redirect must occur exactly once during page2 load',
      );
      session.startTextSearch(
        query.pattern,
        goToFirstMatch: false,
        startPageNumber: target,
      );
      redirected = true;
    }
  };
  void changed() {
    final range = session.getMatchesRangeForPage(targetMatchPage);
    if (firstTargetNotification == null &&
        range != null &&
        range.end > range.start) {
      firstTargetNotification = clock.elapsedMicroseconds;
    }
    if (!session.isSearching && !done.isCompleted) done.complete();
  }

  session.addListener(changed);
  Issue5538WrapMetrics.reset(collect: _instrumented && !correctness);
  try {
    clock.start();
    session.startTextSearch(
      query.pattern,
      goToFirstMatch: false,
      searchImmediately: true,
      startPageNumber: order == 'start75' ? target : null,
    );
    await done.future.timeout(const Duration(minutes: 5));
    clock.stop();
    final elapsed = clock.elapsedMicroseconds;
    final counts = Issue5538WrapMetrics.take();
    Issue5538WrapMetrics.enabled = false;
    await Future.wait(pendingSelections);
    _require(
      session.searchError == null,
      'Search failed: ${session.searchError}',
    );
    _require(
      listEquals(
        session.scanOrder,
        _expectedOrder(texts.length, target, order),
      ),
      'Unexpected page scan order',
    );
    _require(
      redirected == (order == 'redirect'),
      'Missing or unexpected redirect',
    );
    _require(
      _signature(session.matches) == _signature(reference),
      'Ordered geometry/context/result parity failed',
    );
    _require(
      firstTargetNotification != null,
      'Target match was never notified',
    );
    for (final snapshot in retained) {
      _require(
        snapshot.list.length == snapshot.length &&
            _signature(snapshot.list) == snapshot.hash,
        'Previously published match snapshot changed',
      );
    }
    if (correctness) {
      _require(
        selected != null && session.currentIndex != null,
        'Selection was not exercised',
      );
      _require(
        _key(session.matches[session.currentIndex!]) == selected,
        'Current index no longer selects the original result after insertion',
      );
      _require(
        controller.selectedPage ==
            session.matches[session.currentIndex!].pageNumber,
        'Selection page changed',
      );
      _require(retained.isNotEmpty, 'No published snapshot was retained');
    } else {
      _require(
        session.currentIndex == null,
        'goToFirstMatch:false acquired an unexpected selection',
      );
    }
    final ranges = [
      for (var page = 1; page <= texts.length; page++)
        {
          'pageNumber': page,
          'start': session.getMatchesRangeForPage(page)?.start,
          'end': session.getMatchesRangeForPage(page)?.end,
        },
    ];
    var offset = 0;
    for (final range in ranges) {
      final count = reference
          .where((m) => m.pageNumber == range['pageNumber'])
          .length;
      _require(
        range['start'] == offset && range['end'] == offset + count,
        'Per-page range does not match ordered results',
      );
      offset += count;
    }
    return {
      'queryId': query.id,
      'correctnessOnly': correctness,
      'scanOrder': session.scanOrder,
      'redirectCount': redirected ? 1 : 0,
      'resultCount': session.matches.length,
      'resultSha256': _signature(session.matches),
      'pageRanges': ranges,
      'currentIndex': session.currentIndex,
      'selectedKey': selected,
      'retainedSnapshots': [
        for (final snapshot in retained)
          {
            'afterPage': snapshot.afterPage,
            'length': snapshot.length,
            'sha256Before': snapshot.hash,
            'sha256After': _signature(snapshot.list),
          },
      ],
      if (!correctness) ...{
        'sessionElapsedUs': elapsed,
        'firstTargetMatchNotificationUs': firstTargetNotification,
        'firstTargetMatchPage': targetMatchPage,
        'timingClass': _instrumented
            ? 'instrumented-diagnostic'
            : 'unmodified-control',
        'insertionProbes': counts,
      },
    };
  } finally {
    Issue5538WrapMetrics.enabled = false;
    session.removeListener(changed);
    session.dispose();
  }
}

Future<Map<String, Object?>> _run(Directory output) async {
  final env = Platform.environment;
  final input = File(env['WRAP_SEARCH_INPUT']!);
  final inputSha = env['WRAP_SEARCH_SHA256']!;
  final pdfium = File(env['WRAP_SEARCH_PDFIUM']!);
  final order = env['WRAP_SEARCH_ORDER']!;
  _require(await _fileHash(input) == inputSha, 'Corpus identity mismatch');
  _require(await _fileHash(pdfium) == _pdfiumSha, 'PDFium identity mismatch');
  Pdfrx.pdfiumModulePath = await pdfium.resolveSymbolicLinks();
  Pdfrx.normalizeHebrewText = true;
  Pdfrx.decodeLegacyHebrewText = true;
  final cache = await Directory('${output.path}/native-cache').create();
  await pdfrxInitialize(tmpPath: cache.path);
  await SearchEngineLibrary.ensureLoaded();
  final searchEngine = File(
    '${File(Platform.resolvedExecutable).parent.path}/lib/libsearch_engine.so',
  );
  final searchEngineSha = await _fileHash(searchEngine);
  final document = await PdfDocument.openFile(input.path);
  try {
    final texts = <PdfPageText>[];
    for (final page in document.pages) {
      texts.add(await page.loadStructuredText());
    }
    _require(
      texts.length == int.parse(env['WRAP_SEARCH_PAGES']!),
      'Page count mismatch',
    );
    final target = (texts.length * 3 / 4)
        .floor()
        .clamp(3, texts.length)
        .toInt();
    final queries = _queries(texts, target);
    final references = <String, List<PdfTextSearchResult>>{};
    for (final query in queries) {
      references[query.id] = await _reference(texts, query.pattern);
    }
    final descriptors = [
      for (final q in queries) {'id': q.id, ...q.descriptor},
    ];
    final textHashes = [
      for (final text in texts)
        {'pageNumber': text.pageNumber, 'textSha256': _hash(text.fullText)},
    ];
    final referenceRows = [
      for (final q in queries)
        {
          'queryId': q.id,
          'resultSha256': _signature(references[q.id]!),
          'results': references[q.id]!.map(_result).toList(),
        },
    ];
    await _json(File('${output.path}/reference.json'), referenceRows);
    await _json(File('${output.path}/ready.json'), {
      'processId': pid,
      'sourceCommit': _source,
      'variant': _instrumented ? 'counts' : 'control',
      'queryDescriptorsSha256': _hash(descriptors),
      'inputSha256': inputSha,
      'searchEngineSha256': searchEngineSha,
      'pdfiumSha256': _pdfiumSha,
      'extractionFinished': true,
      'measurementsStarted': false,
    });
    await _waitFor(File('${output.path}/go'));
    final observations = <Map<String, Object?>>[];
    final observationsFile = File('${output.path}/observations.jsonl');
    await observationsFile.create(exclusive: true);
    final repetitions = _instrumented ? 1 : 5;
    for (var pass = 0; pass <= repetitions; pass++) {
      for (final query in queries) {
        final row = await _scan(
          document,
          texts,
          query,
          references[query.id]!,
          order,
          target,
          correctness: false,
        );
        final observation = {
          ...row,
          'warmup': pass == 0,
          'iteration': pass == 0 ? 0 : pass - 1,
        };
        observations.add(observation);
        // The session stopwatch is stopped before the row is returned. Keep
        // completed passes even if a later pass or correctness check fails.
        await observationsFile.writeAsString(
          '${jsonEncode(observation)}\n',
          mode: FileMode.append,
          flush: true,
        );
      }
    }
    final correctness = <Map<String, Object?>>[];
    for (final query in queries) {
      final row = await _scan(
        document,
        texts,
        query,
        references[query.id]!,
        order,
        target,
        correctness: true,
      );
      correctness.add(row);
      await _json(File('${output.path}/correctness-${query.id}.json'), row);
    }
    _require(
      await _fileHash(input) == inputSha &&
          await _fileHash(pdfium) == _pdfiumSha,
      'Input/native identity changed during experiment',
    );
    _require(
      await _fileHash(searchEngine) == searchEngineSha,
      'Search engine changed during experiment',
    );
    return {
      'schemaVersion': 1,
      'status': 'passed',
      'component': 'actual-centered-search-session-wrap-insertion',
      'executionMode': 'flutter-profile',
      'sourceCommit': _source,
      'variant': _instrumented ? 'counts' : 'control',
      'processId': pid,
      'host': Platform.localHostname,
      'dartVersion': Platform.version,
      'inputSha256': inputSha,
      'pageCount': texts.length,
      'inputBytes': await input.length(),
      'pdfiumSha256': _pdfiumSha,
      'searchEngineSha256': searchEngineSha,
      'scanCondition': order,
      'targetPage': target,
      'warmup': 1,
      'repetitions': repetitions,
      'queries': descriptors,
      'queryDescriptorsSha256': _hash(descriptors),
      'pageTextHashes': textHashes,
      'pageTextHashesSha256': _hash(textHashes),
      'observations': observations,
      'correctness': correctness,
      'referenceSha256': await _fileHash(File('${output.path}/reference.json')),
      'limitations': [
        'Native extraction and real app query construction occur before the host start handshake and all timings.',
        'The real CenteredTextSearchSession uses preloaded native text through an overridden loadText; its private result buffer and matching algorithm are unchanged in the control.',
        'A document-only controller adapter records selection; viewer rendering, app UI, PDF loading and input latency are not measured.',
        'firstTargetMatchNotificationUs includes the public notification schedule; it is not the exact private insertion instant.',
        'Count probes and their durations are diagnostic; copied references are logical elements, not measured allocated heap bytes.',
        'Repeated passes within one process are dependent; the host summarizes independent process medians.',
      ],
    };
  } finally {
    await document.dispose();
  }
}

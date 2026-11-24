import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
// ignore: depend_on_referenced_packages
import 'package:pdfrx_engine/pdfrx_engine.dart';

import 'internal_text_access.dart';

Future<String> _hash(File file) async =>
    (await sha256.bind(file.openRead()).single).toString();

void _json(File file, Object value) => file.writeAsStringSync(
  '${const JsonEncoder.withIndent('  ').convert(value)}\n',
  flush: true,
);

Map<String, Object?> _proof(PdfPageRawText? text) => {
  'hasRawText': text != null,
  'utf16CodeUnits': text?.fullText.length ?? 0,
  'characterRects': text?.charRects.length ?? 0,
  'textSha256': sha256.convert(utf8.encode(text?.fullText ?? '')).toString(),
  'geometrySha256': sha256
      .convert(
        utf8.encode(
          jsonEncode([
            for (final rect in text?.charRects ?? <PdfRect>[])
              [rect.left, rect.top, rect.right, rect.bottom],
          ]),
        ),
      )
      .toString(),
};

Future<void> main(List<String> arguments) async {
  final config = jsonDecode(await File(arguments.single).readAsString()) as Map;
  final output = Directory(config['output'] as String);
  if (output.existsSync() || Platform.environment['CI'] != 'true') {
    throw StateError('A new output directory and CI=true are required.');
  }
  output.createSync(recursive: true);
  const instrumented = bool.fromEnvironment('TEXT_INSTRUMENTED');
  const source = String.fromEnvironment('TEXT_SOURCE_COMMIT');
  PdfDocument? document;
  var workerStopped = false;
  final rows = <Map<String, Object?>>[];
  try {
    final input = File(config['input'] as String);
    final pdfium = File(config['pdfium'] as String);
    if (source != config['sourceCommit'] ||
        await _hash(input) != config['inputSha256'] ||
        await _hash(pdfium) != config['pdfiumSha256']) {
      throw StateError(
        'Compiled source, native engine or corpus identity differs.',
      );
    }
    Pdfrx.pdfiumModulePath = pdfium.path;
    Pdfrx.normalizeHebrewText = true;
    Pdfrx.decodeLegacyHebrewText = true;
    await pdfrxInitialize(tmpPath: '${output.path}/cache');
    document = await PdfDocument.openFile(input.path);
    if (document.pages.length != config['pageCount'] ||
        document.pages.any((page) => !page.isLoaded)) {
      throw StateError('The complete real document was not loaded.');
    }
    final warmup = config['warmup'] as int;
    final repetitions = config['repetitions'] as int;
    for (var iteration = 0; iteration < warmup + repetitions; iteration++) {
      await takeTextMetrics(reset: true);
      final pass = <Map<String, Object?>>[];
      for (final page in document.pages) {
        final clock = Stopwatch()..start();
        final raw = await page.loadText();
        clock.stop();
        pass.add({
          'iteration': iteration,
          'warmup': iteration < warmup,
          'pageNumber': page.pageNumber,
          'callerRawLoadUs': clock.elapsedMicroseconds,
          ..._proof(raw),
        });
      }
      final metrics = await takeTextMetrics(reset: true);
      if (metrics.length != (instrumented ? pass.length : 0)) {
        throw StateError('Worker metrics do not cover exactly this page pass.');
      }
      for (var index = 0; index < pass.length; index++) {
        final row = pass[index];
        if (instrumented) {
          final worker = metrics[index];
          if (worker['pageNumber'] != row['pageNumber'] ||
              worker['completed'] != true ||
              worker['normalizeHebrew'] != true) {
            throw StateError('Worker observation identity differs.');
          }
          row['worker'] = worker;
          row['outsideMeasuredWorkerBodyUs'] =
              (row['callerRawLoadUs'] as int) - (worker['workerBodyUs'] as int);
          if ((row['outsideMeasuredWorkerBodyUs'] as int) < 0) {
            throw StateError(
              'Measured worker duration exceeds the caller interval.',
            );
          }
        }
        rows.add(row);
        File('${output.path}/observations.jsonl').writeAsStringSync(
          '${jsonEncode(row)}\n',
          mode: FileMode.append,
        );
      }
    }
    final referencePage = config['referencePage'] as int;
    final page = document.pages[referencePage - 1];
    final normalized = await page.loadText();
    PdfPageRawText? unnormalized;
    try {
      Pdfrx.normalizeHebrewText = false;
      Pdfrx.decodeLegacyHebrewText = false;
      unnormalized = await page.loadText();
    } finally {
      Pdfrx.normalizeHebrewText = true;
      Pdfrx.decodeLegacyHebrewText = true;
    }
    final normalizedText = normalized?.fullText ?? '';
    final rawText = unnormalized?.fullText ?? '';
    final hebrew = RegExp(r'[\u05D0-\u05EA]');
    final marks = RegExp(
      r'[\u0591-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]',
    );
    if (rawText.length < 1000 ||
        hebrew.allMatches(rawText).length < 100 ||
        marks.allMatches(rawText).isEmpty) {
      throw StateError(
        'The normalization reference lacks real pointed Hebrew content.',
      );
    }
    await File(
      '${output.path}/reference-normalized.txt',
    ).writeAsString(normalizedText);
    await File(
      '${output.path}/reference-before-normalization.txt',
    ).writeAsString(rawText);
    final reference = {
      'pageNumber': referencePage,
      'timed': false,
      'normalized': {
        ..._proof(normalized),
        'hebrewMarks': marks.allMatches(normalizedText).length,
      },
      'beforeHebrewTransforms': {
        ..._proof(unnormalized),
        'hebrewMarks': marks.allMatches(rawText).length,
      },
      'scope':
          'PDFium extraction including inferred spacing; not raw PDF object bytes',
      'sourcePdfEncodedSpaceClaim': false,
    };
    await document.dispose();
    document = null;
    await PdfrxEntryFunctions.instance.stopBackgroundWorker();
    workerStopped = true;
    _json(File('${output.path}/report.json'), {
      'measurementComplete': true,
      'executionMode': const String.fromEnvironment('TEXT_MODE'),
      'instrumented': instrumented,
      'sourceCommit': source,
      'inputSha256': config['inputSha256'],
      'pdfiumSha256': config['pdfiumSha256'],
      'pageCount': config['pageCount'],
      'warmup': warmup,
      'repetitions': repetitions,
      'observations': rows,
      'reference': reference,
      'limitations': [
        'Worker raw-build includes PDFium calls and Dart text/rectangle construction.',
        'The outside-worker residual includes queue, dispatch, arena management, return and instrumentation bookkeeping; it is not isolated transfer time.',
        'Worker counters are read/reset through a separate job outside each timed page interval.',
        'Serial page calls do not measure a concurrent render/search backlog.',
        'Instrumented durations include observer overhead and are not a product speedup.',
        'No native-allocation, Dart-allocation, UI-latency or cold OS-cache claim.',
      ],
    });
  } catch (error, stack) {
    _json(File('${output.path}/failure.json'), {
      'error': '$error',
      'stack': '$stack',
    });
    rethrow;
  } finally {
    await document?.dispose();
    if (!workerStopped) {
      await PdfrxEntryFunctions.instance.stopBackgroundWorker();
    }
  }
}

import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
// Measures the engine already pinned transitively by pdfrx.
// ignore: depend_on_referenced_packages
import 'package:pdfrx_engine/pdfrx_engine.dart';

import 'src/pdf_text_profile.dart';
import 'src/pdf_text_profile_report.dart';

const externalTextQueries = [
  PdfTextProfileQuery.literal('literal_elohim', 'אלהים'),
  PdfTextProfileQuery.regex(
    'pointed_elohim',
    r'א[\u0591-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]*'
        r'ל[\u0591-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]*'
        r'ה[\u0591-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]*'
        r'י[\u0591-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]*ם',
  ),
  PdfTextProfileQuery.regex(
    'hebrew_combining_marks',
    r'[\u0591-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]',
  ),
  PdfTextProfileQuery.literal('no_match', 'otzaria-probe-absent-5538'),
  PdfTextProfileQuery.regex('zero_length', r'(?=א)'),
];

Future<void> main(List<String> arguments) async {
  Directory? output;
  PdfDocument? document;
  RandomAccessFile? stream;
  final cleanupErrors = <String>[];
  try {
    final options = _arguments(arguments);
    output = Directory(options['output']!);
    if (await output.exists()) {
      throw ArgumentError('Output directory must be new');
    }
    await output.create(recursive: true);
    final pdfium = File(options['pdfium']!);
    final original = File(options['input-pdf']!);
    final inputHash = options['input-sha256']!;
    final pdfiumHash = options['pdfium-sha256']!;
    final warmup = int.parse(options['warmup'] ?? '1');
    final repetitions = int.parse(options['repetitions'] ?? '7');
    if (warmup < 0 || repetitions < 1) {
      throw ArgumentError('Invalid warmup/repetitions');
    }
    if (await _hash(original) != inputHash ||
        await _hash(pdfium) != pdfiumHash) {
      throw StateError('Input PDF or PDFium differs from its expected SHA-256');
    }
    final input = await original.copy('${output.path}/corpus.pdf');
    final cache = await Directory('${output.path}/native-cache').create();
    Pdfrx.pdfiumModulePath = await pdfium.resolveSymbolicLinks();
    Pdfrx.normalizeHebrewText = true;
    Pdfrx.decodeLegacyHebrewText = true;
    await pdfrxInitialize(tmpPath: cache.path);
    document = await PdfDocument.openFile(input.path);
    final textDirectory = await Directory('${output.path}/text').create();
    PdfPageRawText? rawBeforeNormalization;
    try {
      Pdfrx.normalizeHebrewText = false;
      Pdfrx.decodeLegacyHebrewText = false;
      rawBeforeNormalization = await document.pages.first.loadText();
    } finally {
      Pdfrx.normalizeHebrewText = true;
      Pdfrx.decodeLegacyHebrewText = true;
    }
    final rawFile = File(
      '${textDirectory.path}/raw-page-1-before-hebrew-normalization.txt',
    );
    final rawContent = rawBeforeNormalization?.fullText ?? '';
    await rawFile.writeAsString(rawContent, flush: true);
    String excerpt(String text) =>
        text.substring(0, text.length < 350 ? text.length : 350);
    String? structuredPage1Excerpt;
    final inventory = <Map<String, Object?>>[];
    final marks = externalTextQueries[2].pattern as RegExp;
    for (final page in document.pages) {
      final text = await page.loadStructuredText();
      if (page.pageNumber == 1) structuredPage1Excerpt = excerpt(text.fullText);
      final file = File('${textDirectory.path}/${page.pageNumber}.txt');
      await file.writeAsString(text.fullText, flush: true);
      inventory.add({
        'pageNumber': page.pageNumber,
        'width': page.width,
        'height': page.height,
        'rotation': page.rotation.index,
        'textFile': 'text/${page.pageNumber}.txt',
        'textSha256': await _hash(file),
        'textCharacters': text.fullText.length,
        'hebrewCombiningMarks': marks.allMatches(text.fullText).length,
        'characterRects': text.charRects.length,
        'fragments': text.fragments.length,
      });
    }
    final corpus = {
      'kind': 'external-pdf',
      'label': options['document-label'],
      'sha256': inputHash,
      'bytes': await input.length(),
      'pageCount': document.pages.length,
      'inventory': inventory,
      'textAccuracyGroundTruthAvailable': false,
      'whitespaceModified': false,
      'queryPolicy':
          'fixed literal and combining-mark tolerant regex; no space removal',
    };
    await _writeJson(File('${output.path}/corpus.json'), corpus);
    stream = File(
      '${output.path}/observations.jsonl',
    ).openSync(mode: FileMode.writeOnly);
    final profile = await runPdfTextProfile(
      document: document,
      queries: externalTextQueries,
      warmup: warmup,
      repetitions: repetitions,
      onObservation: (row) => stream!.writeStringSync('${jsonEncode(row)}\n'),
    );
    validatePdfTextProfileReport(profile);
    for (final row in profile['observations'] as List) {
      final page = inventory[(row['pageNumber'] as int) - 1];
      if (row['textSha256'] != page['textSha256'] ||
          row['textCharacters'] != page['textCharacters']) {
        throw StateError(
          'Measured text differs from the preserved native text',
        );
      }
    }
    stream.closeSync();
    stream = null;
    await document.dispose();
    document = null;
    await PdfrxEntryFunctions.instance.stopBackgroundWorker();
    if (await _hash(original) != inputHash ||
        await _hash(input) != inputHash ||
        await _hash(pdfium) != pdfiumHash) {
      throw StateError('Input PDF or PDFium changed during the run');
    }
    final firstPipeline = (profile['observations'] as List).where(
      (row) => row['iteration'] == 0 && row['phase'] == 'pipeline',
    );
    await _writeJson(File('${output.path}/report.json'), {
      'protocolVersion': 1,
      'status': 'passed',
      'component': 'pdfrx-public-text-pipeline-external-corpus',
      'executionMode': const String.fromEnvironment(
        'PDF_TEXT_PROFILE_MODE',
        defaultValue: 'dart-jit-validation',
      ),
      'dartVersion': Platform.version,
      'operatingSystem': Platform.operatingSystem,
      'operatingSystemVersion': Platform.operatingSystemVersion,
      'processors': Platform.numberOfProcessors,
      'pdfiumSha256': pdfiumHash,
      'pdfiumBytes': await pdfium.length(),
      'corpus': corpus,
      'profile': profile,
      'textObservations': {
        'queryMatchesPerCompletePass': {
          for (final query in externalTextQueries)
            query.id: firstPipeline.fold<int>(
              0,
              (sum, row) =>
                  sum +
                  ((row['queries'] as List).singleWhere(
                        (value) => value['id'] == query.id,
                      )['matches']
                      as int),
            ),
        },
        'structuredPage1Excerpt': structuredPage1Excerpt,
        'rawBeforeHebrewNormalization': {
          'pageNumber': 1,
          'normalizeHebrewText': false,
          'decodeLegacyHebrewText': false,
          'hasRawText': rawBeforeNormalization != null,
          'timed': false,
          'scope':
              'PdfPage.loadText before Hebrew transforms; PDFium extraction and inferred spacing remain',
          'file': 'text/raw-page-1-before-hebrew-normalization.txt',
          'sha256': await _hash(rawFile),
          'characters': rawContent.length,
          'excerpt': excerpt(rawContent),
        },
        'sourcePdfEncodedSpaceClaims': false,
      },
      'uiLatencyCollected': false,
      'nativeTransferSeparated': false,
      'osCacheCleared': false,
    });
    stdout.writeln(
      jsonEncode({
        'status': 'passed',
        'output': output.absolute.path,
        'corpusSha256': inputHash,
        'summary': profile['summary'],
      }),
    );
  } catch (error, stack) {
    for (final close in <Future<void> Function()>[
      () async => stream?.closeSync(),
      () async => document?.dispose(),
      () => PdfrxEntryFunctions.instance.stopBackgroundWorker(),
    ]) {
      try {
        await close();
      } catch (failure) {
        cleanupErrors.add(failure.toString());
      }
    }
    stderr.writeln('$error\n$stack');
    if (output != null && await output.exists()) {
      await _writeJson(File('${output.path}/failure.json'), {
        'status': 'failed',
        'error': error.toString(),
        'stack': stack.toString(),
        'cleanupErrors': cleanupErrors,
      });
    }
    exitCode = 1;
  }
}

Map<String, String> _arguments(List<String> arguments) {
  const allowed = {
    'output',
    'input-pdf',
    'input-sha256',
    'document-label',
    'pdfium',
    'pdfium-sha256',
    'warmup',
    'repetitions',
  };
  final result = <String, String>{};
  for (var index = 0; index < arguments.length; index += 2) {
    final name = arguments[index];
    if (!name.startsWith('--') ||
        !allowed.contains(name.substring(2)) ||
        index + 1 == arguments.length ||
        result.containsKey(name.substring(2))) {
      throw ArgumentError('Unknown, duplicate or incomplete argument: $name');
    }
    result[name.substring(2)] = arguments[index + 1];
  }
  for (final name in allowed.difference({'warmup', 'repetitions'})) {
    if (result[name]?.isNotEmpty != true) {
      throw ArgumentError('Missing --$name');
    }
  }
  for (final name in ['input-sha256', 'pdfium-sha256']) {
    if (!RegExp(r'^[0-9a-f]{64}$').hasMatch(result[name]!)) {
      throw ArgumentError('Invalid --$name');
    }
  }
  return result;
}

Future<String> _hash(File file) async =>
    (await sha256.bind(file.openRead()).single).toString();

Future<void> _writeJson(File file, Object value) => file.writeAsString(
  '${const JsonEncoder.withIndent('  ').convert(value)}\n',
  flush: true,
);

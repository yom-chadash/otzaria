// Fork-only large real-document export experiment for issue 5538.
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:otzaria/printing/pdf_export_cancellation.dart';
import 'package:otzaria/printing/pdf_sheet_composer.dart';
import 'package:pdfrx/pdfrx.dart';

Future<String> _hash(File file) async =>
    (await sha256.bind(file.openRead()).first).toString();

Future<void> _json(File file, Object value) async {
  final pending = File('${file.path}.pending');
  await pending.writeAsString('${jsonEncode(value)}\n', flush: true);
  await pending.rename(file.path);
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const SizedBox.shrink());
  if (!kProfileMode || !const bool.fromEnvironment('PDF_5538_EXPORT_PROBE')) {
    throw StateError('This fork-only export probe requires profile mode.');
  }
  final configFile = File(Platform.environment['PDF_5538_EXPORT_CONFIG']!);
  final config = jsonDecode(await configFile.readAsString()) as Map;
  final output = Directory(config['output'] as String);
  try {
    final result = await _measure(config, output);
    await _json(File('${output.path}/result.json'), result);
    exit(0);
  } catch (error, stack) {
    await _json(File('${output.path}/failure.json'), {
      'status': 'failed',
      'error': error.toString(),
      'stack': stack.toString(),
    });
    exit(1);
  }
}

Future<Map<String, Object?>> _measure(Map config, Directory output) async {
  final input = File(config['input'] as String);
  final module = File(config['pdfium'] as String);
  final inputHash = await _hash(input);
  if (inputHash != config['inputSha256'] ||
      await _hash(module) != config['pdfiumSha256']) {
    throw StateError('The real corpus or native library changed.');
  }
  final variant = config['variant'] as String;
  if (!['bytes', 'file', 'cancel', 'write-failure'].contains(variant)) {
    throw ArgumentError('Unknown export variant.');
  }
  Pdfrx.pdfiumModulePath = module.path;
  final nativeCache = await Directory('${output.path}/native-cache').create();
  await pdfrxInitialize(tmpPath: nativeCache.path);
  final openClock = Stopwatch()..start();
  final document = await PdfDocument.openFile(input.path);
  openClock.stop();
  final count = math.min(config['pages'] as int, document.pages.length);
  final destination = File('${output.path}/export.pdf');
  final token = PdfExportCancellationToken();
  Timer? cancellation;
  final ready = {
    'pid': pid,
    'mode': 'profile',
    'sourceCommit': config['sourceCommit'],
    'variant': variant,
    'inputSha256': inputHash,
    'inputBytes': await input.length(),
    'inputPages': document.pages.length,
    'exportedPages': count,
    'inputOpenUs': openClock.elapsedMicroseconds,
    'rssBeforeBytes': ProcessInfo.currentRss,
  };
  await _json(File('${output.path}/ready.json'), ready);
  final deadline = DateTime.now().add(const Duration(seconds: 60));
  while (!await File('${output.path}/start').exists()) {
    if (DateTime.now().isAfter(deadline)) {
      throw StateError('Host did not verify the mapped engine.');
    }
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
  var peakRss = ProcessInfo.currentRss;
  var samples = 0;
  final sampleTimer = Timer.periodic(const Duration(milliseconds: 5), (_) {
    peakRss = math.max(peakRss, ProcessInfo.currentRss);
    samples++;
  });
  Uint8List? bytes;
  var length = 0;
  String? expectedFailure;
  final composeClock = Stopwatch()..start();
  await _json(File('${output.path}/phase.json'), {
    'phase': 'compose',
    'epochUs': DateTime.now().microsecondsSinceEpoch,
  });
  try {
    if (variant == 'bytes') {
      bytes = await composePdfSheets(
        document,
        firstIndex: 0,
        lastIndex: count - 1,
        rows: 2,
        cols: 2,
        sheetWidth: 595,
        sheetHeight: 842,
      );
      length = bytes.length;
    } else {
      if (variant == 'cancel') {
        cancellation = Timer(const Duration(milliseconds: 5), token.cancel);
      }
      length = await composePdfSheetsToFile(
        document,
        outputPath: variant == 'write-failure' ? '/dev/full' : destination.path,
        firstIndex: 0,
        lastIndex: count - 1,
        rows: 2,
        cols: 2,
        sheetWidth: 595,
        sheetHeight: 842,
        cancellationToken: token,
      );
    }
  } on PdfExportCancelledException {
    if (variant != 'cancel') rethrow;
    expectedFailure = 'cancelled';
  } on FileSystemException {
    if (variant != 'write-failure') rethrow;
    expectedFailure = 'write-failed';
  } finally {
    composeClock.stop();
    cancellation?.cancel();
    token.dispose();
    sampleTimer.cancel();
  }
  peakRss = math.max(peakRss, ProcessInfo.currentRss);
  await _json(File('${output.path}/phase.json'), {
    'phase': 'composed',
    'epochUs': DateTime.now().microsecondsSinceEpoch,
  });
  final rssAfterCompose = ProcessInfo.currentRss;
  final diskClock = Stopwatch();
  try {
    if (variant == 'cancel' || variant == 'write-failure') {
      if (expectedFailure == null) {
        throw StateError('The injected cancellation/write failure succeeded.');
      }
      // Reuse the still-owned source after the failure, including its native data.
      final sourceProof = await _pageProof(document.pages[count ~/ 2]);
      return {
        ...ready,
        'status': 'passed',
        'expectedFailure': expectedFailure,
        'composeUs': composeClock.elapsedMicroseconds,
        'sampledRssPeakBytes': peakRss,
        'rssSampleCount': samples,
        'sourceStillUsable': sourceProof,
        'partialFileBytes': await destination.exists()
            ? await destination.length()
            : 0,
      };
    }
    if (bytes != null) {
      diskClock.start();
      await destination.writeAsBytes(bytes, flush: true);
      diskClock.stop();
    }
    if (length != await destination.length() || length <= 16 * 1024 * 1024) {
      throw StateError('The large export is missing, truncated, or <=16 MiB.');
    }
    final exported = await PdfDocument.openFile(destination.path);
    try {
      if (exported.pages.length != (count + 3) ~/ 4) {
        throw StateError('N-up export page count differs.');
      }
      final textDigests = <String>[];
      for (final page in exported.pages) {
        final text = (await page.loadText())?.fullText ?? '';
        textDigests.add(sha256.convert(utf8.encode(text)).toString());
      }
      final selected = {
        0,
        exported.pages.length ~/ 2,
        exported.pages.length - 1,
      };
      final pixels = <Map<String, Object?>>[];
      for (final index in selected) {
        pixels.add(await _pageProof(exported.pages[index]));
      }
      return {
        ...ready,
        'status': 'passed',
        'composeUs': composeClock.elapsedMicroseconds,
        'bytesResultWriteUs': diskClock.elapsedMicroseconds,
        'rssAfterComposeBytes': rssAfterCompose,
        'sampledRssPeakBytes': peakRss,
        'rssSampleCount': samples,
        'sampleIntervalMs': 5,
        'outputBytes': length,
        'outputSha256': await _hash(destination),
        'outputPages': exported.pages.length,
        'allPagesTextSha256': textDigests,
        'sampledPages': pixels,
        'retainedDartOutputBytes': bytes?.length ?? 0,
        'limitations': [
          'Timer and host RSS peaks are lower bounds between samples.',
          'PDFium document graphs remain resident in both variants.',
          'Byte composition excludes its additional disk write, reported separately.',
          'Visual parity samples first/middle/last; text parity covers every output page.',
        ],
      };
    } finally {
      await exported.dispose();
    }
  } finally {
    await document.dispose();
    if (await destination.exists()) await destination.delete();
  }
}

Future<Map<String, Object?>> _pageProof(PdfPage page) async {
  final image = await page.render(
    width: 300,
    height: 400,
    fullWidth: 300,
    fullHeight: 400,
    backgroundColor: 0xffffffff,
  );
  if (image == null) throw StateError('No raster for page ${page.pageNumber}.');
  try {
    final text = (await page.loadText())?.fullText ?? '';
    return {
      'pageNumber': page.pageNumber,
      'width': page.width,
      'height': page.height,
      'pixelsSha256': sha256.convert(image.pixels).toString(),
      'textSha256': sha256.convert(utf8.encode(text)).toString(),
    };
  } finally {
    image.dispose();
  }
}

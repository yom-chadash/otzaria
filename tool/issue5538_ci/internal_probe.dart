import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:crypto/crypto.dart';
// ignore: depend_on_referenced_packages
import 'package:pdfrx_engine/pdfrx_engine.dart';
// ignore: depend_on_referenced_packages, implementation_imports
import 'package:pdfrx_engine/src/native/worker.dart';

import 'internal_fonts.dart';
import 'internal_http.dart';
import 'internal_http_revalidation.dart';
import 'internal_metrics_access.dart';

typedef Observe =
    Future<void> Function(
      String section,
      String workload,
      Map<String, Object?> configuration,
      Future<Map<String, Object?>> Function() action,
    );

Future<String> fileHash(File file) async =>
    (await sha256.bind(file.openRead()).single).toString();

List<int> cpuTicks() {
  final stat = File('/proc/self/stat').readAsStringSync();
  final fields = stat.substring(stat.lastIndexOf(')') + 2).split(' ');
  return [int.parse(fields[11]), int.parse(fields[12])];
}

Future<void> main(List<String> arguments) async {
  final options = <String, List<String>>{};
  for (var i = 0; i < arguments.length; i += 2) {
    if (!arguments[i].startsWith('--') || i + 1 >= arguments.length) {
      throw ArgumentError('Arguments must be --name value pairs');
    }
    options
        .putIfAbsent(arguments[i].substring(2), () => [])
        .add(arguments[i + 1]);
  }
  final output = Directory(options['output']!.single);
  final suite = options['suite']?.single ?? 'internals';
  if (suite != 'internals' && suite != 'http-revalidation') {
    throw ArgumentError.value(suite, 'suite');
  }
  if (output.existsSync() || Platform.environment['CI'] != 'true') {
    throw StateError('A new output directory and CI=true are required');
  }
  output.createSync(recursive: true);
  final observations = <Map<String, Object?>>[];
  final cleanupErrors = <String>[];
  final clockTicks = int.parse(
    (await Process.run('getconf', ['CLK_TCK'])).stdout.toString().trim(),
  );
  Future<void> observe(
    String section,
    String workload,
    Map<String, Object?> configuration,
    Future<Map<String, Object?>> Function() action,
  ) async {
    await takeInternalMetrics(reset: true);
    final before = cpuTicks();
    final rss = ProcessInfo.currentRss;
    final elapsed = Stopwatch()..start();
    final result = await action();
    elapsed.stop();
    final after = cpuTicks();
    final row = <String, Object?>{
      'section': section,
      'workload': workload,
      ...configuration,
      ...result,
      'measurementComplete': true,
      'wallUs': elapsed.elapsedMicroseconds,
      'cpuUserUs': (after[0] - before[0]) * 1000000 ~/ clockTicks,
      'cpuSystemUs': (after[1] - before[1]) * 1000000 ~/ clockTicks,
      'cpuResolutionUs': 1000000 ~/ clockTicks,
      'rssBefore': rss,
      'rssAfter': ProcessInfo.currentRss,
      'processLifetimePeakRss': ProcessInfo.maxRss,
      'metrics': await takeInternalMetrics(),
    };
    observations.add(row);
    File('${output.path}/observations.jsonl').writeAsStringSync(
      '${jsonEncode(row)}\n',
      mode: FileMode.append,
      flush: true,
    );
  }

  try {
    Pdfrx.pdfiumModulePath = options['pdfium']!.single;
    Pdfrx.normalizeHebrewText = true;
    Pdfrx.decodeLegacyHebrewText = true;
    Pdfrx.cacheDirectoryPath = '${output.path}/cache';
    await pdfrxInitialize(tmpPath: Pdfrx.cacheDirectoryPath);
    if (suite == 'http-revalidation') {
      await httpRevalidationObservations(
        output,
        File(options['http-pdf']!.single),
        File(options['http-linearized']!.single),
        observe,
      );
    } else {
      await schedulerObservations(options['pdf']!, observe);
      await httpObservations(
        File(options['http-pdf']!.single),
        File(options['http-linearized']!.single),
        observe,
      );
      await fontObservations(output, File(options['font']!.single), observe);
    }
  } catch (error, stack) {
    File(
      '${output.path}/failure.json',
    ).writeAsStringSync(jsonEncode({'error': '$error', 'stack': '$stack'}));
    rethrow;
  } finally {
    try {
      await PdfrxEntryFunctions.instance.stopBackgroundWorker();
    } catch (error) {
      cleanupErrors.add('$error');
    }
  }
  final report = <String, Object?>{
    'protocolVersion': 1,
    'suite': suite,
    'measurementComplete': cleanupErrors.isEmpty,
    'executionMode': const String.fromEnvironment('INTERNAL_MODE'),
    'instrumented': const bool.fromEnvironment('INTERNAL_INSTRUMENTED'),
    'instrumentedTimesAreProductTimes': false,
    'dartVersion': Platform.version,
    'processors': Platform.numberOfProcessors,
    'operatingSystemVersion': Platform.operatingSystemVersion,
    'uiLatencyCollected': false,
    'osCacheCleared': false,
    'physicalDevice': false,
    'processWideGate': Pdfrx.useProcessWidePdfiumGate,
    'pdfiumSha256': await fileHash(File(options['pdfium']!.single)),
    'observations': observations,
    'cleanupErrors': cleanupErrors,
  };
  File('${output.path}/report.json').writeAsStringSync(
    const JsonEncoder.withIndent('  ').convert(report),
    flush: true,
  );
  if (cleanupErrors.isNotEmpty) throw StateError('Native cleanup failed');
  stdout.writeln(
    jsonEncode({
      'measurementComplete': true,
      'observations': observations.length,
    }),
  );
}

Future<void> schedulerObservations(List<String> pdfs, Observe observe) async {
  for (final tasks in [8, 64, 256, 1024, 4096]) {
    await observe(
      'M01',
      'worker-burst',
      {'tasks': tasks, 'warmup': tasks == 8},
      () async {
        final latencies = <int>[];
        final results = await Future.wait(
          List.generate(tasks, (i) async {
            final timer = Stopwatch()..start();
            final result = await BackgroundWorker.compute((int n) => n, i);
            latencies.add(timer.elapsedMicroseconds);
            return result;
          }),
        );
        if (results.asMap().entries.any((entry) => entry.key != entry.value)) {
          throw StateError('Worker result identity mismatch');
        }
        return {
          'latencyUs': latencies,
          'completed': results.length,
          'nativePdfWork': false,
        };
      },
    );
  }
  for (final path in pdfs) {
    final file = File(path);
    for (final pass in [0, 1]) {
      await observe(
        'M01',
        'native-pdf-mixed',
        {
          'pdfSha256': await fileHash(file),
          'fileBytes': file.lengthSync(),
          'warmProcessPass': pass,
          'pdfWorkload':
              'concurrent extraction, low-priority raster, visible raster, canceled raster',
        },
        () async {
          final document = await PdfDocument.openFile(path);
          try {
            final count = document.pages.length;
            final selected = List.generate(
              min(count, 32),
              (i) => count == 1 ? 0 : i * (count - 1) ~/ (min(count, 32) - 1),
            );
            final completed = <Map<String, Object?>>[];
            final futures = <Future<void>>[];
            for (final index in selected) {
              futures.add(() async {
                final text = await Pdfrx.runAsBackgroundWork(
                  () => document.pages[index].loadText(),
                );
                completed.add({
                  'kind': 'text',
                  'page': index + 1,
                  'textSha256': sha256
                      .convert(utf8.encode(text?.fullText ?? ''))
                      .toString(),
                  'characters': text?.fullText.length ?? 0,
                });
              }());
            }
            for (var i = 0; i < min(selected.length, 8); i++) {
              final page = document.pages[selected[i]];
              final token = page.createCancellationToken();
              final canceled = i.isOdd;
              final future = page.render(
                fullWidth: 384,
                fullHeight: 384 * page.height / page.width,
                cancellationToken: token,
                priority: i == 0
                    ? PdfPageRenderPriority.high
                    : PdfPageRenderPriority.low,
              );
              if (canceled) token.cancel();
              futures.add(() async {
                final image = await future;
                try {
                  completed.add({
                    'kind': 'raster',
                    'page': page.pageNumber,
                    'cancelRequested': canceled,
                    'returnedImage': image != null,
                    'pixels': image == null ? 0 : image.width * image.height,
                    if (image != null)
                      'pixelSha256': sha256.convert(image.pixels).toString(),
                  });
                } finally {
                  image?.dispose();
                }
              }());
            }
            await Future.wait(futures);
            return {
              'pageCount': count,
              'completionOrder': completed,
              'documentDisposed': true,
            };
          } finally {
            await document.dispose();
          }
        },
      );
    }
  }
}

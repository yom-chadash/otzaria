import 'dart:async';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
// ignore: depend_on_referenced_packages
import 'package:pdfrx_engine/pdfrx_engine.dart';
// ignore: depend_on_referenced_packages, implementation_imports
import 'package:pdfrx_engine/src/native/pdf_file_cache.dart';

import 'internal_probe.dart' show Observe, fileHash;

class _Server {
  _Server(this.bytes, this.latencyMs, this.bytesPerSecond, this.forceFull);
  final Uint8List bytes;
  final int latencyMs;
  final int bytesPerSecond;
  final bool forceFull;
  final requests = <Map<String, Object?>>[];
  final pending = <Future<void>>{};
  late HttpServer server;
  var active = 0;
  var maxActive = 0;

  Future<void> start() async {
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    server.listen((request) {
      final future = handle(request);
      pending.add(future);
      unawaited(future.whenComplete(() => pending.remove(future)));
    });
  }

  Uri uri(String path) =>
      Uri.parse('http://127.0.0.1:${server.port}/$path.pdf');

  Future<void> handle(HttpRequest request) async {
    final timer = Stopwatch()..start();
    active++;
    maxActive = max(maxActive, active);
    final range = request.headers.value('range');
    var start = 0;
    var end = bytes.length;
    final match = range == null
        ? null
        : RegExp(r'^bytes=(\d+)-(\d+)$').firstMatch(range);
    final partial = !forceFull && match != null;
    if (partial) {
      start = int.parse(match.group(1)!);
      end = min(bytes.length, int.parse(match.group(2)!) + 1);
    }
    final row = <String, Object?>{
      'path': request.uri.path,
      'range': range,
      'start': start,
      'endExclusive': end,
      'status': partial ? 206 : 200,
      'payloadBytes': 0,
      'completed': false,
      'hasAuthorization': request.headers.value('authorization') != null,
    };
    requests.add(row);
    try {
      if (latencyMs > 0) {
        await Future<void>.delayed(Duration(milliseconds: latencyMs));
      }
      request.response.statusCode = partial ? 206 : 200;
      request.response.headers.contentType = ContentType('application', 'pdf');
      request.response.headers.set('cache-control', 'public, max-age=3600');
      request.response.headers.set('etag', '"issue5538-fixed-source"');
      if (partial) {
        request.response.headers.set(
          'content-range',
          'bytes $start-${end - 1}/${bytes.length}',
        );
      }
      request.response.contentLength = end - start;
      for (var position = start; position < end; position += 65536) {
        final length = min(65536, end - position);
        request.response.add(
          Uint8List.sublistView(bytes, position, position + length),
        );
        await request.response.flush();
        row['payloadBytes'] = (row['payloadBytes'] as int) + length;
        if (bytesPerSecond > 0) {
          await Future<void>.delayed(
            Duration(microseconds: length * 1000000 ~/ bytesPerSecond),
          );
        }
      }
      await request.response.close();
      row['completed'] = true;
    } on Object catch (error) {
      row['transportError'] = '$error';
    } finally {
      row['wallUs'] = timer.elapsedMicroseconds;
      active--;
    }
  }

  Future<void> close() async {
    await server.close(force: true);
    await Future.wait(pending.toList());
  }
}

Future<void> httpObservations(
  File original,
  File linearized,
  Observe observe,
) async {
  for (final entry in [(original, false), (linearized, true)]) {
    final bytes = await entry.$1.readAsBytes();
    for (final configuration in [
      (65536, 0, 0, false),
      (1048576, 0, 0, false),
      (65536, 15, 8 * 1024 * 1024, false),
      (65536, 15, 8 * 1024 * 1024, true),
    ]) {
      final server = _Server(
        bytes,
        configuration.$2,
        configuration.$3,
        configuration.$4,
      );
      await server.start();
      final uri = server.uri('document');
      final rasterHashes = <String>[];
      try {
        for (final warmCache in [false, true]) {
          final offset = server.requests.length;
          await observe(
            'E10',
            'native-http-first-page',
            {
              'pdfSha256': await fileHash(entry.$1),
              'linearized': entry.$2,
              'blockSize': configuration.$1,
              'latencyMs': configuration.$2,
              'bytesPerSecond': configuration.$3,
              'forceHttp200': configuration.$4,
              'warmCache': warmCache,
              'inputBytes': bytes.length,
              'inputResidentInServer': true,
            },
            () async {
              final timer = Stopwatch()..start();
              final document = await pdfDocumentFromUri(
                uri,
                blockSize: configuration.$1,
                useRangeAccess: true,
              );
              final openUs = timer.elapsedMicroseconds;
              try {
                final page = document.pages.first;
                final image = await page.render(
                  fullWidth: 384,
                  fullHeight: 384 * page.height / page.width,
                );
                if (image == null) {
                  throw StateError('HTTP PDF did not render its first page');
                }
                final firstRasterUs = timer.elapsedMicroseconds;
                final fingerprint = sha256.convert(image.pixels).toString();
                image.dispose();
                rasterHashes.add(fingerprint);
                await Future.wait(server.pending.toList());
                final path = await PdfFileCache.getCacheFilePathForUri(uri);
                final requests = server.requests
                    .skip(offset)
                    .map((r) => Map<String, Object?>.of(r))
                    .toList();
                return {
                  'openUs': openUs,
                  'firstRasterUs': firstRasterUs,
                  'pageCount': document.pages.length,
                  'pixelSha256': fingerprint,
                  'sameRasterAsCold': rasterHashes.first == fingerprint,
                  'requests': requests,
                  'requestCount': requests.length,
                  'payloadBytes': requests.fold<int>(
                    0,
                    (n, r) => n + (r['payloadBytes'] as int),
                  ),
                  'maxConcurrentServerRequests': server.maxActive,
                  'cacheFileLogicalBytes': path.existsSync()
                      ? path.lengthSync()
                      : null,
                };
              } finally {
                await document.dispose();
              }
            },
          );
        }
      } finally {
        await server.close();
      }
    }
  }
  await overlapAndQuota(original, observe);
}

class _ReadCapture extends PdfrxEntryFunctions {
  late FutureOr<int> Function(Uint8List, int, int) reader;
  @override
  Future<PdfDocument> openCustom({
    required FutureOr<int> Function(Uint8List buffer, int position, int size)
    read,
    required int fileSize,
    required String sourceName,
    PdfPasswordProvider? passwordProvider,
    bool firstAttemptByEmptyPassword = true,
    bool useProgressiveLoading = false,
    int? maxSizeToCacheOnMemory,
    void Function()? onDispose,
  }) async {
    reader = read;
    return _ReadLifetime(onDispose);
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _ReadLifetime extends PdfDocument {
  _ReadLifetime(this.release) : super(sourceName: 'http-cache-read-capture');
  final void Function()? release;
  @override
  Future<void> dispose() async => release?.call();
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

Future<void> overlapAndQuota(File input, Observe observe) async {
  final bytes = await input.readAsBytes();
  if (bytes.length < 3 * 65536) {
    throw StateError('HTTP corpus is too small for overlapping block reads');
  }
  final server = _Server(bytes, 10, 0, false);
  await server.start();
  try {
    await observe(
      'E10',
      'overlapping-cache-reads',
      {
        'pdfSha256': await fileHash(input),
        'concurrentReads': 20,
        'scope':
            'real PdfFileCache reader with capture entry; no native PDFium in this subcase',
      },
      () async {
        final capture = _ReadCapture();
        final doc = await pdfDocumentFromUri(
          server.uri('overlap'),
          blockSize: 65536,
          entryFunctions: capture,
        );
        final offset = server.requests.length;
        const start = 65536 + 32;
        const size = 4096;
        try {
          final actual = await Future.wait(
            List.generate(20, (_) async {
              final buffer = Uint8List(size);
              final read = await capture.reader(buffer, start, size);
              return {
                'bytesRead': read,
                'sha256': sha256.convert(buffer).toString(),
              };
            }),
          );
          final expected = sha256
              .convert(Uint8List.sublistView(bytes, start, start + size))
              .toString();
          final requests = server.requests
              .skip(offset)
              .map((r) => Map<String, Object?>.of(r))
              .toList();
          return {
            'requests': requests,
            'requestsForOneMissingBlock': requests.length,
            'payloadBytes': requests.fold<int>(
              0,
              (n, r) => n + (r['payloadBytes'] as int),
            ),
            'expectedReadSha256': expected,
            'reads': actual,
            'allReadsCorrect': actual.every(
              (r) => r['sha256'] == expected && r['bytesRead'] == size,
            ),
            'singleFlightObserved': requests.length == 1,
            'maxConcurrentServerRequests': server.maxActive,
          };
        } finally {
          await doc.dispose();
        }
      },
    );
    await observe(
      'E10',
      'cache-growth-and-auth-identity',
      {'documents': 8, 'quotaConfigured': false},
      () async {
        final snapshots = <Map<String, Object?>>[];
        for (var i = 0; i < 8; i++) {
          final capture = _ReadCapture();
          final doc = await pdfDocumentFromUri(
            server.uri('retained-$i'),
            blockSize: 65536,
            entryFunctions: capture,
          );
          await doc.dispose();
          final files = Directory(
            Pdfrx.cacheDirectoryPath!,
          ).listSync(recursive: true).whereType<File>().toList();
          snapshots.add({
            'completedDocuments': i + 1,
            'files': files.length,
            'logicalBytes': files.fold<int>(0, (n, f) => n + f.lengthSync()),
          });
        }
        final a = await PdfFileCache.getCacheFilePathForUri(
          server.uri('auth'),
          headers: {'Authorization': 'Bearer probe-A'},
        );
        final b = await PdfFileCache.getCacheFilePathForUri(
          server.uri('auth'),
          headers: {'Authorization': 'Bearer probe-B'},
        );
        return {
          'diskGrowth': snapshots,
          'authIdentitiesHaveDistinctPaths': a.path != b.path,
          'automaticQuotaClaim': false,
          'observationDoesNotEstablishLongTermDiskBound': true,
        };
      },
    );
  } finally {
    await server.close();
  }
}

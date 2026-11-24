import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
// ignore: depend_on_referenced_packages
import 'package:pdfrx_engine/pdfrx_engine.dart';
// ignore: depend_on_referenced_packages, implementation_imports
import 'package:pdfrx_engine/src/native/pdf_file_cache.dart';

import 'internal_probe.dart' show Observe, fileHash;

const revalidationPolicies = <String, (bool, bool, bool)>{
  'etag-fresh': (true, true, false),
  'etag-stale': (true, false, true),
  'modified-since-stale': (false, false, true),
  'etag-must-revalidate-fresh': (true, true, true),
};

class RevalidationServer {
  RevalidationServer(
    this.bytes, {
    required this.policy,
    this.documentId = 'document',
  }) : resourceSha256 = sha256.convert(bytes).toString();

  Uint8List bytes;
  final String policy;
  final String documentId;
  String resourceSha256;
  var version = 1;
  final requests = <Map<String, Object?>>[];
  final pending = <Future<void>>{};
  final errors = <String>[];
  late HttpServer server;

  String? get etag =>
      revalidationPolicies[policy]!.$1 ? '"e10-v$version"' : null;
  DateTime get lastModified => DateTime.utc(2020, 1, version);
  bool get fresh => revalidationPolicies[policy]!.$2;
  String get cacheControl =>
      'public, max-age=${fresh ? 3600 : 0}'
      '${revalidationPolicies[policy]!.$3 ? ', must-revalidate' : ''}';

  Future<void> start() async {
    if (!revalidationPolicies.containsKey(policy)) {
      throw ArgumentError.value(policy, 'policy');
    }
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    server.autoCompress = false;
    server.listen((request) {
      final future = handle(request);
      pending.add(future);
      unawaited(future.then((_) => pending.remove(future)));
    });
  }

  Uri get uri => Uri.parse('http://127.0.0.1:${server.port}/$documentId.pdf');

  void replace(Uint8List replacement) {
    if (pending.isNotEmpty) throw StateError('HTTP responses are still active');
    bytes = replacement;
    resourceSha256 = sha256.convert(replacement).toString();
    version++;
  }

  bool notModified(String? ifNoneMatch, String? ifModifiedSince) {
    if (ifNoneMatch != null) {
      return ifNoneMatch.split(',').any((entry) {
        final tag = entry.trim().replaceFirst(RegExp(r'^W/'), '');
        return tag == '*' || (etag != null && tag == etag);
      });
    }
    if (ifModifiedSince == null) return false;
    try {
      return !lastModified.isAfter(HttpDate.parse(ifModifiedSince));
    } on HttpException {
      return false;
    } on FormatException {
      return false;
    }
  }

  Future<void> handle(HttpRequest request) async {
    final timer = Stopwatch()..start();
    final ifNoneMatch = request.headers.value('if-none-match');
    final ifModifiedSince = request.headers.value('if-modified-since');
    final range = request.headers.value('range');
    final responseDate = DateTime.now().toUtc().subtract(
      fresh ? Duration.zero : const Duration(hours: 1),
    );
    final responseHeaders = <String, String>{
      'date': HttpDate.format(responseDate),
      'expires': HttpDate.format(
        responseDate.add(
          fresh ? const Duration(hours: 1) : Duration.zero,
        ),
      ),
      'cache-control': cacheControl,
      'last-modified': HttpDate.format(lastModified),
      'etag': ?etag,
      'accept-ranges': 'bytes',
    };
    final conditional = ifNoneMatch != null || ifModifiedSince != null;
    final matched = conditional && notModified(ifNoneMatch, ifModifiedSince);
    final row = <String, Object?>{
      'method': request.method,
      'path': request.uri.path,
      'range': range,
      'ifNoneMatch': ifNoneMatch,
      'ifModifiedSince': ifModifiedSince,
      'conditionalRequest': conditional,
      'validatorMatched': matched,
      'resourceVersion': version,
      'resourceBytes': bytes.length,
      'resourceSha256': resourceSha256,
      'responseHeaders': responseHeaders,
      'payloadBytes': 0,
      'completed': false,
    };
    requests.add(row);
    try {
      responseHeaders.forEach(request.response.headers.set);
      if (request.method != 'GET' || request.uri.path != uri.path) {
        throw StateError(
          'Unexpected HTTP request: ${request.method} ${request.uri}',
        );
      }
      if (matched) {
        request.response.statusCode = HttpStatus.notModified;
        row['status'] = HttpStatus.notModified;
        row['payloadSha256'] = sha256.convert(const <int>[]).toString();
      } else {
        final match = range == null
            ? null
            : RegExp(r'^bytes=(\d+)-(\d+)$').firstMatch(range);
        if (range != null && match == null) {
          throw StateError('Unsupported Range: $range');
        }
        final start = match == null ? 0 : int.parse(match.group(1)!);
        final end = match == null
            ? bytes.length
            : min(bytes.length, int.parse(match.group(2)!) + 1);
        if (start < 0 || start >= end) {
          throw StateError('Invalid Range: $range');
        }
        final status = match == null
            ? HttpStatus.ok
            : HttpStatus.partialContent;
        request.response.statusCode = status;
        request.response.headers.contentType = ContentType(
          'application',
          'pdf',
        );
        request.response.contentLength = end - start;
        if (match != null) {
          final value = 'bytes $start-${end - 1}/${bytes.length}';
          request.response.headers.set('content-range', value);
          responseHeaders['content-range'] = value;
        }
        row['status'] = status;
        row['start'] = start;
        row['endExclusive'] = end;
        row['payloadSha256'] = sha256
            .convert(Uint8List.sublistView(bytes, start, end))
            .toString();
        request.response.add(Uint8List.sublistView(bytes, start, end));
        await request.response.flush();
        row['payloadBytes'] = end - start;
      }
      await request.response.close();
      row['completed'] = true;
    } on Object catch (error) {
      row['transportError'] = '$error';
      errors.add('$error');
      try {
        await request.response.close();
      } on Object catch (closeError) {
        errors.add('Response close failed: $closeError');
      }
    } finally {
      row['wallUs'] = timer.elapsedMicroseconds;
    }
  }

  Future<void> drain() async {
    while (pending.isNotEmpty) {
      await Future.wait(pending.toList());
    }
    if (errors.isNotEmpty) throw StateError('HTTP server errors: $errors');
  }

  Future<void> close() async {
    await server.close(force: true);
    await Future.wait(pending.toList());
  }
}

Future<Map<String, Object?>> _raster(
  PdfPage page, {
  void Function()? rendered,
}) async {
  final image = await page.render(
    fullWidth: 384,
    fullHeight: 384 * page.height / page.width,
  );
  if (image == null) {
    throw StateError('HTTP PDF did not render page ${page.pageNumber}');
  }
  rendered?.call();
  try {
    return {
      'page': page.pageNumber,
      'pageWidth': page.width,
      'pageHeight': page.height,
      'width': image.width,
      'height': image.height,
      'pixelSha256': sha256.convert(image.pixels).toString(),
    };
  } finally {
    image.dispose();
  }
}

Future<Map<String, Object?>> _reference(File file) async {
  final document = await PdfDocument.openFile(file.path);
  try {
    final pages = <int>{
      0,
      document.pages.length ~/ 2,
      document.pages.length - 1,
    };
    return {
      'pdfSha256': await fileHash(file),
      'pageCount': document.pages.length,
      'rasters': [
        for (final index in pages) await _raster(document.pages[index]),
      ],
    };
  } finally {
    await document.dispose();
  }
}

Future<Map<String, Object?>> _cacheReceipt(Uri uri, Uint8List expected) async {
  final file = await PdfFileCache.getCacheFilePathForUri(uri);
  if (!file.existsSync()) throw StateError('No HTTP cache file');
  final cache = await PdfFileCache.fromFile(file);
  try {
    if (!cache.isInitialized) {
      throw StateError('HTTP cache did not retain its metadata');
    }
    final blocks = <Map<String, Object?>>[];
    var parity = cache.fileSize == expected.length;
    var residentBytes = 0;
    for (var i = 0; i < cache.totalBlocks; i++) {
      if (!cache.isCached(i)) continue;
      final offset = i * cache.blockSize;
      final size = min(cache.blockSize, cache.fileSize - offset);
      final actual = Uint8List(size);
      await cache.read(actual, 0, offset, size);
      final actualHash = sha256.convert(actual).toString();
      final expectedHash = offset + size <= expected.length
          ? sha256
                .convert(Uint8List.sublistView(expected, offset, offset + size))
                .toString()
          : null;
      final matches = actualHash == expectedHash;
      parity = parity && matches;
      residentBytes += size;
      blocks.add({
        'index': i,
        'bytes': size,
        'actualSha256': actualHash,
        'expectedSha256': expectedHash,
        'matches': matches,
      });
    }
    final state = cache.cacheControlState;
    final now = DateTime.now().toUtc();
    return {
      'fileSize': cache.fileSize,
      'blockSize': cache.blockSize,
      'totalBlocks': cache.totalBlocks,
      'cachedBlocks': blocks.length,
      'residentBytes': residentBytes,
      'fullyCached': cache.isFullyCached,
      'allResidentBytesMatchInput': parity,
      'blocks': blocks,
      'cacheFileLogicalBytes': file.lengthSync(),
      'cacheFileSha256': await fileHash(file),
      'cacheControl': state.cacheControl.toString(),
      'etag': state.etag,
      'lastModified': state.lastModified?.toUtc().toIso8601String(),
      'date': state.date?.toUtc().toIso8601String(),
      'expires': state.expires?.toUtc().toIso8601String(),
      'observedAt': now.toIso8601String(),
      'engineReportsFresh': state.isFresh(now: now),
    };
  } finally {
    await cache.close();
  }
}

Future<void> httpRevalidationObservations(
  Directory output,
  File original,
  File linearized,
  Observe observe,
) async {
  final files = [original, linearized];
  final bytes = [await original.readAsBytes(), await linearized.readAsBytes()];
  final references = [await _reference(original), await _reference(linearized)];
  if (references[0]['pdfSha256'] == references[1]['pdfSha256']) {
    throw StateError('Changed-resource case needs distinct real PDF bytes');
  }
  File('${output.path}/http-local-references.json').writeAsStringSync(
    jsonEncode(references),
    flush: true,
  );
  for (var initial = 0; initial < files.length; initial++) {
    for (final progressive in [false, true]) {
      for (final policy in revalidationPolicies.keys) {
        final server = RevalidationServer(
          bytes[initial],
          policy: policy,
          documentId: 'case-$initial-$progressive-$policy',
        );
        await server.start();
        final phases = [
          'cold',
          'warm',
          'warm-late-pages',
          if (!server.fresh) ...['changed', 'changed-warm'],
        ];
        Map<String, Object?>? cacheBefore;
        var selected = initial;
        try {
          for (final phase in phases) {
            if (phase == 'changed') {
              selected = 1 - initial;
              server.replace(bytes[selected]);
            }
            final offset = server.requests.length;
            final configuration = <String, Object?>{
              'policy': policy,
              'phase': phase,
              'initialLinearized': initial == 1,
              'linearized': selected == 1,
              'pdfSha256': references[selected]['pdfSha256'],
              'inputBytes': bytes[selected].length,
              'resourceVersion': server.version,
              'blockSize': 65536,
              'progressive': progressive,
              'uri': server.uri.toString(),
              'warmCache': phase != 'cold',
              'cacheBefore': cacheBefore,
              'serverFresh': server.fresh,
              'referencePreloadedInProcess': true,
              'scope':
                  'native Dart AOT; first 384px raster, not UI time-to-sharp; '
                  'cache verification and late-page rasters are outside firstRasterUs',
            };
            try {
              await observe(
                'E10',
                'http-revalidation',
                configuration,
                () async {
                  final timer = Stopwatch()..start();
                  final document = await pdfDocumentFromUri(
                    server.uri,
                    blockSize: 65536,
                    useProgressiveLoading: progressive,
                    useRangeAccess: true,
                  );
                  final openUs = timer.elapsedMicroseconds;
                  late int firstRasterUs;
                  late int pageCount;
                  final rasters = <Map<String, Object?>>[];
                  try {
                    pageCount = document.pages.length;
                    rasters.add(
                      await _raster(
                        document.pages.first,
                        rendered: () =>
                            firstRasterUs = timer.elapsedMicroseconds,
                      ),
                    );
                    if (phase == 'warm-late-pages' || phase == 'changed') {
                      for (final index in <int>{
                        pageCount ~/ 2,
                        pageCount - 1,
                      }..remove(0)) {
                        rasters.add(await _raster(document.pages[index]));
                      }
                    }
                  } finally {
                    await document.dispose();
                  }
                  await server.drain();
                  final cacheAfter = await _cacheReceipt(
                    server.uri,
                    bytes[selected],
                  );
                  final requests = server.requests
                      .skip(offset)
                      .map((r) => Map<String, Object?>.of(r))
                      .toList();
                  final expectedRasters =
                      references[selected]['rasters']!
                          as List<Map<String, Object?>>;
                  final rasterParity =
                      pageCount == references[selected]['pageCount'] &&
                      rasters.every(
                        (actual) => expectedRasters.any(
                          (expected) =>
                              jsonEncode(actual) == jsonEncode(expected),
                        ),
                      );
                  final payload = requests.fold<int>(
                    0,
                    (total, request) =>
                        total + (request['payloadBytes']! as int),
                  );
                  final valid304 = requests
                      .where((r) => r['status'] == 304)
                      .length;
                  cacheBefore = cacheAfter;
                  return {
                    'openUs': openUs,
                    'firstRasterUs': firstRasterUs,
                    'pageCount': pageCount,
                    'rasters': rasters,
                    'rasterParityWithLocalFile': rasterParity,
                    'bytesParity': cacheAfter['allResidentBytesMatchInput'],
                    'cacheAfter': cacheAfter,
                    'requests': requests,
                    'requestCount': requests.length,
                    'conditionalRequests': requests
                        .where((r) => r['conditionalRequest'] == true)
                        .length,
                    'notModifiedResponses': valid304,
                    'payloadBytes': payload,
                    'noNetworkReuseObserved':
                        phase != 'cold' && requests.isEmpty,
                    'revalidatedReuseObserved': valid304 > 0 && payload == 0,
                    'revalidatedSparseRefillObserved':
                        valid304 > 0 && payload > 0,
                    'serverErrors': List<String>.of(server.errors),
                  };
                },
              );
            } on Object catch (error, stack) {
              File(
                '${output.path}/http-revalidation-failure.json',
              ).writeAsStringSync(
                jsonEncode({
                  'configuration': configuration,
                  'error': '$error',
                  'stack': '$stack',
                  'requests': server.requests.skip(offset).toList(),
                  'serverErrors': server.errors,
                }),
                flush: true,
              );
              rethrow;
            }
          }
        } finally {
          await server.close();
        }
      }
    }
  }
}

import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'internal_http_revalidation.dart';

void check(bool condition, String message) {
  if (!condition) throw StateError(message);
}

Future<void> main() async {
  final client = HttpClient();
  var cases = 0;
  Future<void> verify(
    RevalidationServer server,
    int expectedStatus,
    Map<String, String> headers,
    List<int> expectedBytes,
  ) async {
    final request = await client.getUrl(server.uri);
    headers.forEach(request.headers.set);
    final response = await request.close();
    final body = await response.fold<List<int>>(
      [],
      (bytes, part) => bytes..addAll(part),
    );
    await server.drain();
    check(
      response.statusCode == expectedStatus,
      'Expected $expectedStatus, got ${response.statusCode} for $headers',
    );
    check(
      jsonEncode(body) == jsonEncode(expectedBytes),
      'Response bytes changed',
    );
    check(
      response.headers.value('last-modified') ==
          HttpDate.format(server.lastModified),
      'Last-Modified must describe the selected representation',
    );
    check(
      response.headers.value('etag') == server.etag,
      'ETag was not sent faithfully',
    );
    final row = server.requests.last;
    for (final header
        in (row['responseHeaders']! as Map<String, String>).entries) {
      check(
        response.headers.value(header.key) == header.value,
        'Response header ${header.key} differs from its receipt',
      );
    }
    check(
      row['status'] == expectedStatus &&
          row['payloadBytes'] == body.length &&
          row['completed'] == true,
      'Server receipt differs from client response',
    );
    if (expectedStatus == 304) {
      check(
        row['conditionalRequest'] == true && row['validatorMatched'] == true,
        '304 requires an actual matching conditional request',
      );
      check(
        response.headers.value('content-range') == null,
        '304 must not carry a range',
      );
    }
    cases++;
  }

  final server = RevalidationServer(
    Uint8List.fromList([1, 2, 3, 4]),
    policy: 'etag-fresh',
  );
  await server.start();
  try {
    await verify(server, 200, {}, [1, 2, 3, 4]);
    await verify(server, 206, {'range': 'bytes=1-2'}, [2, 3]);
    await verify(server, 304, {
      'range': 'bytes=1-2',
      'if-none-match': server.etag!,
    }, []);
    await verify(server, 304, {'if-none-match': 'W/${server.etag}'}, []);
    await verify(server, 304, {'if-none-match': '"other", ${server.etag}'}, []);
    await verify(server, 304, {'if-none-match': '*'}, []);
    await verify(
      server,
      206,
      {
        'range': 'bytes=0-1',
        'if-none-match': '"wrong"',
        'if-modified-since': HttpDate.format(DateTime.utc(2030)),
      },
      [1, 2],
    );
    await verify(server, 304, {
      'if-modified-since': HttpDate.format(server.lastModified),
    }, []);
    await verify(
      server,
      200,
      {'if-modified-since': HttpDate.format(DateTime.utc(2019))},
      [1, 2, 3, 4],
    );
    await verify(
      server,
      200,
      {'if-modified-since': 'invalid-date'},
      [1, 2, 3, 4],
    );
    final oldTag = server.etag!;
    final oldDate = HttpDate.format(server.lastModified);
    server.replace(Uint8List.fromList([9, 8, 7, 6, 5]));
    await verify(
      server,
      206,
      {
        'range': 'bytes=0-3',
        'if-none-match': oldTag,
        'if-modified-since': oldDate,
      },
      [9, 8, 7, 6],
    );
    await verify(server, 304, {'if-none-match': server.etag!}, []);
  } finally {
    await server.close();
  }
  final modified = RevalidationServer(
    Uint8List.fromList([5, 6, 7]),
    policy: 'modified-since-stale',
  );
  await modified.start();
  try {
    await verify(modified, 200, {}, [5, 6, 7]);
    final previous = HttpDate.format(modified.lastModified);
    await verify(modified, 304, {'if-modified-since': previous}, []);
    final date = HttpDate.parse(
      (modified.requests.last['responseHeaders']!
          as Map<String, String>)['date']!,
    );
    check(
      DateTime.now().toUtc().difference(date) > const Duration(minutes: 59),
      'Stale case must be stale independently of execution speed',
    );
    modified.replace(Uint8List.fromList([8, 9]));
    await verify(modified, 200, {'if-modified-since': previous}, [8, 9]);
  } finally {
    await modified.close();
    client.close(force: true);
  }
  stdout.writeln(jsonEncode({'serverContractCasesPassed': cases}));
}

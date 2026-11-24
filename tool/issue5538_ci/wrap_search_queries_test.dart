// Fork-only early functional gate; no measurements or synthetic replacement
// for the native corpus selection used by the profile target.
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:otzaria/pdf_book/utils/pdf_user_regex_pattern.dart';
import 'package:pdfrx/pdfrx.dart';

import '../../test/support/search_engine_test_init.dart';
import 'wrap_search_queries.dart';

String _hash(Object? value) =>
    sha256.convert(utf8.encode(jsonEncode(value))).toString();

Future<String> _fileHash(File file) async =>
    (await sha256.bind(file.openRead()).first).toString();

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late Map<String, dynamic> config;
  late String searchEngineSha;

  setUpAll(() async {
    final path = Platform.environment['WRAP_SEARCH_QUERY_PREFLIGHT'];
    expect(path, isNotNull, reason: 'CI must provide pinned corpus inputs');
    config = jsonDecode(await File(path!).readAsString())
        as Map<String, dynamic>;
    expect(await tryInitSearchEngine(), isTrue, reason: searchEngineSkipReason);
    expect(searchEngineLoadedLibraryPath, isNotNull);
    searchEngineSha = await _fileHash(File(searchEngineLoadedLibraryPath!));
    final pdfium = File(config['pdfium'] as String);
    expect(await _fileHash(pdfium), config['pdfiumSha256']);
    Pdfrx.pdfiumModulePath = await pdfium.resolveSymbolicLinks();
    Pdfrx.normalizeHebrewText = true;
    Pdfrx.decodeLegacyHebrewText = true;
    final cache = await Directory('${config['output']}.native-cache').create();
    await pdfrxInitialize(tmpPath: cache.path);
  });

  test('fixed candidates use the actual single-word user-regex parser', () {
    for (final word in wrapSearchCandidates) {
      final queries = buildWrapSearchCandidate(word);
      expect(queries.map((query) => query.id), [
        'app-simple',
        'app-user-regex',
      ]);
      expect(queries[1].pattern, isA<PdfUserRegexPattern>());
      expect(queries[1].descriptor['query'], '/$word/');
      expect(queries[1].descriptor['planKind'], 'singleWord');
      expect(queries[1].pattern.hasMatch(' $word '), isTrue);
    }
  });

  test(
    'real UXLC and MG select positive fixed queries before either AOT build',
    () async {
      final rows = <Map<String, Object?>>[];
      for (final value in config['documents'] as List<dynamic>) {
        final item = value as Map<String, dynamic>;
        final input = File(item['absolutePath'] as String);
        expect(await _fileHash(input), item['sha256']);
        expect(await input.length(), item['sizeBytes']);
        final document = await PdfDocument.openFile(input.path);
        try {
          expect(document.pages.length, item['pageCount']);
          final texts = <PdfPageText>[];
          for (final page in document.pages) {
            texts.add(await page.loadStructuredText());
          }
          final target = texts.length * 3 ~/ 4;
          final queries = buildWrapSearchQueries(texts, target);
          final descriptors = [
            for (final query in queries) {'id': query.id, ...query.descriptor},
          ];
          final hashes = [
            for (final text in texts)
              {
                'pageNumber': text.pageNumber,
                'textSha256': _hash(text.fullText),
              },
          ];
          rows.add({
            'id': item['id'],
            'inputSha256': await _fileHash(input),
            'pageCount': texts.length,
            'targetPage': target,
            'queries': descriptors,
            'queryDescriptorsSha256': _hash(descriptors),
            'pageTextHashesSha256': _hash(hashes),
          });
        } finally {
          await document.dispose();
        }
      }
      expect(await _fileHash(File(searchEngineLoadedLibraryPath!)),
          searchEngineSha);
      expect(await _fileHash(File(config['pdfium'] as String)),
          config['pdfiumSha256']);
      final output = File(config['output'] as String);
      expect(await output.exists(), isFalse);
      await output.writeAsString('${jsonEncode({
        'schemaVersion': 1,
        'status': 'passed',
        'scope': 'Debug/JIT functional query gate; no performance samples',
        'sourceCommit': config['sourceCommit'],
        'processId': pid,
        'pdfiumSha256': config['pdfiumSha256'],
        'searchEngineSha256': searchEngineSha,
        'searchEnginePath': searchEngineLoadedLibraryPath,
        'documents': rows,
      })}\n', flush: true);
    },
    timeout: const Timeout(Duration(minutes: 5)),
  );
}

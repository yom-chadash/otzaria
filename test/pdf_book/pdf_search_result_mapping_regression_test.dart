import 'package:flutter_test/flutter_test.dart';
import 'package:otzaria/pdf_book/utils/pdf_search_result_list.dart';
import 'package:otzaria/pdf_book/view/pdf_search_screen.dart';
import 'package:otzaria_search_engine/otzaria_search_engine.dart';
import 'package:pdfrx/pdfrx.dart';

void main() {
  test('wraparound search constructs each result only once', () {
    final factory = _ResultFactory();
    final results = PdfSearchResultList<SearchResult>(factory.create);
    for (final matches in _snapshots(1000, 10)) {
      results.update(matches, generation: 1);
      for (var i = 0; i < results.length; i++) {
        results[i];
      }
      expect(factory.created, matches.length);
    }
    expect(results, hasLength(10000));
    expect(factory.created, 10000);
  });

  test('a visible window does not construct offscreen search results', () {
    final factory = _ResultFactory();
    final results = PdfSearchResultList<SearchResult>(factory.create);
    for (final matches in _snapshots(1000, 10)) {
      results.update(matches, generation: 1);
      final start = results.visualIndexForPage(501);
      expect(results.groupedItems[start], 501);
      for (var i = start; i < start + 20; i++) {
        results.groupedItems[i];
      }
      expect(factory.created, inInclusiveRange(1, 20));
    }
    expect(results, hasLength(10000));
    expect(factory.created, lessThanOrEqualTo(20));
  });
}

List<List<PdfTextSearchResult>> _snapshots(int pages, int matchesPerPage) {
  const phrase = 'שלום עולם ומלואו ';
  final pageText = List.filled(matchesPerPage, phrase).join();
  final matches = List.generate(pages, (page) {
    final text = PdfPageText(
      pageNumber: page + 1,
      fullText: pageText,
      charRects: List.filled(pageText.length, const PdfRect(0, 1, 1, 0)),
      fragments: const [],
    );
    return List.generate(
      matchesPerPage,
      (i) => PdfTextSearchResult.fromRange(
        PdfPageTextRange(
          pageText: text,
          start: i * phrase.length,
          end: i * phrase.length + 4,
        ),
      ),
    );
  });
  final scanned = <int>{};
  return [
    for (var batch = 0; batch < pages ~/ 10; batch++)
      (() {
        for (var i = 0; i < 10; i++) {
          scanned.add((pages ~/ 2 + batch * 10 + i) % pages);
        }
        final sorted = scanned.toList()..sort();
        return [for (final page in sorted) ...matches[page]];
      })(),
  ];
}

class _ResultFactory {
  int created = 0;

  SearchResult create(PdfTextSearchResult match, int id) {
    final snippet = PdfBookSearchView.matchLineSnippet(match, 'שלום');
    created++;
    return SearchResult(
      id: BigInt.from(id),
      title: 'ספר',
      reference: '',
      text: snippet,
      segment: BigInt.from(match.pageNumber - 1),
      isPdf: true,
      filePath: '/book.pdf',
      mergedCount: 1,
      merged: const [],
      textStatus: TextStatus.ok,
      continuesToNextLine: false,
    );
  }
}

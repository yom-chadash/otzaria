import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:otzaria/pdf_book/utils/proximity_highlight_pattern.dart';
import 'package:otzaria/pdf_book/view/pdf_search_screen.dart';
import 'package:otzaria/search/in_book_search_settings.dart';
import 'package:otzaria/search/models/search_configuration.dart';
import 'package:pdfrx/pdfrx.dart';

typedef PdfHighlightProfileQuery = ({
  String id,
  Pattern pattern,
  Map<String, Object?> descriptor,
});

const pdfProximityProfileSuite = 'product-proximity-v1';
const pdfProximitySelectionRecipe =
    'first-two-fixed-real-word-pairs-with-positive-ordered-distance-2-span-v1';

// Fixed before collecting timings. Selection uses positive matches only, never
// elapsed time, match density, candidate speed, or a repaired OCR/text layer.
const _candidatePairs = <List<String>>[
  ['אבן', 'עזרא'],
  ['ויאמר', 'אל'],
  ['יום', 'זה'],
  ['את', 'יום'],
  ['כי', 'יום'],
  ['כי', 'כל'],
  ['את', 'אשר'],
  ['יום', 'אחד'],
  ['אשר', 'את'],
  ['על', 'כל'],
  ['אל', 'יום'],
  ['כי', 'לא'],
];

class PdfProximityProfileQueries {
  const PdfProximityProfileQueries(this.queries, this.selection);

  final List<PdfHighlightProfileQuery> queries;
  final Map<String, Object?> selection;
}

/// Run after native text extraction and search-library initialization, and
/// before every timed pass. Snippets are derived from real native text; this
/// does not run the search index or claim end-to-end search latency.
PdfProximityProfileQueries buildPdfProximityProfileQueries(
  List<PdfPageText> texts,
) {
  final selected = <({int candidateIndex, PdfPageText text, List<int> span})>[];
  final attempts = <Map<String, Object?>>[];
  for (var index = 0; index < _candidatePairs.length; index++) {
    final terms = _candidatePairs[index];
    final query = searchableInBookQuery(terms.join(' '))!;
    final settings = _settings(query, distance: 2, anyOrder: false);
    final probe = ProximityHighlightPattern(query, settings);
    PdfPageText? witness;
    List<int>? firstSpan;
    var pagesExamined = 0;
    for (final text in texts) {
      pagesExamined++;
      final spans = probe.spans(text.fullText);
      if (spans.isNotEmpty) {
        witness = text;
        firstSpan = spans.first;
        break;
      }
    }
    attempts.add({
      'candidateIndex': index,
      'query': query,
      'pagesExamined': pagesExamined,
      'positive': witness != null,
      'witnessPageNumber': witness?.pageNumber,
    });
    if (witness != null && firstSpan != null) {
      selected.add((candidateIndex: index, text: witness, span: firstSpan));
      if (selected.length == 2) break;
    }
  }
  if (selected.length != 2) {
    throw StateError(
      'Need two positive real-word pairs without changing the corpus: '
      '${jsonEncode(attempts)}',
    );
  }

  final queries = <PdfHighlightProfileQuery>[];
  final witnesses = <Map<String, Object?>>[];
  for (var pairIndex = 0; pairIndex < selected.length; pairIndex++) {
    final item = selected[pairIndex];
    final terms = _candidatePairs[item.candidateIndex];
    final text = item.text.fullText;
    final start = item.span[0];
    final end = item.span[1];
    if (start < 0 || end <= start || end > text.length) {
      throw StateError('Invalid native-text selection span');
    }
    // Retain surrounding characters as well as the exact selected span. No
    // joining split words, whitespace normalization, or OCR repair is applied.
    final snippetStart = start > 80 ? start - 80 : 0;
    final snippetEnd = end + 80 < text.length ? end + 80 : text.length;
    final snippetText = text.substring(snippetStart, snippetEnd);
    final snippetHtml = const HtmlEscape(
      HtmlEscapeMode.element,
    ).convert(snippetText);
    final witness = <String, Object?>{
      'pairIndex': pairIndex + 1,
      'candidateIndex': item.candidateIndex,
      'terms': terms,
      'pageNumber': item.text.pageNumber,
      'pageTextSha256': _hash(text),
      'span': [start, end],
      'spanText': text.substring(start, end),
      'snippetStart': snippetStart,
      'snippetEnd': snippetEnd,
      'snippetText': snippetText,
      'snippetTextSha256': _hash(snippetText),
      'snippetHtml': snippetHtml,
      'snippetHtmlSha256': _hash(snippetHtml),
      'snippetOrigin': 'derived-native-page-text; not an index search result',
    };
    witnesses.add(witness);
    for (final distance in [2, 10]) {
      for (final anyOrder in [false, true]) {
        final queryTerms = anyOrder ? terms.reversed.toList() : terms;
        final query = searchableInBookQuery(queryTerms.join(' '))!;
        final settings = _settings(
          query,
          distance: distance,
          anyOrder: anyOrder,
        );
        if (settings.isSimpleSearch) {
          throw StateError('Proximity workload routed to simple search');
        }
        final pattern = PdfBookSearchView.buildAdvancedHighlightPattern(
          [snippetHtml],
          query: query,
          settings: settings,
        );
        if (pattern is! ProximityHighlightPattern ||
            pattern.query != query ||
            pattern.key != ProximityHighlightPattern(query, settings).key) {
          throw StateError('Actual app builder did not select Proximity');
        }
        final overlaps = [
          for (final span in pattern.spans(text))
            if (span[0] < end && span[1] > start) span,
        ];
        if (overlaps.isEmpty) {
          throw StateError('Actual app pattern missed its native-text witness');
        }
        queries.add((
          id: 'proximity_pair_${pairIndex + 1}_d${distance}_'
              '${anyOrder ? 'any_order_reversed' : 'ordered'}',
          pattern: pattern,
          descriptor: <String, Object?>{
            'kind': 'ProximityHighlightPattern',
            'builder': 'PdfBookSearchView.buildAdvancedHighlightPattern',
            'observedPatternType': pattern.runtimeType.toString(),
            'productRoute': 'advanced-search-page-highlights',
            'pairIndex': pairIndex + 1,
            'candidateIndex': item.candidateIndex,
            'terms': queryTerms,
            'query': pattern.query,
            'queryOrdering': anyOrder ? 'reversed' : 'original',
            'patternKey': pattern.key,
            'isSimpleSearch': settings.isSimpleSearch,
            'settings': {
              'searchMode': settings.searchMode.name,
              'distance': settings.distance,
              'proximityScope': settings.matchPolicy.proximityScope.name,
              'wordMatchMode': settings.matchPolicy.wordMatchMode.name,
              'wordMatchCount': settings.matchPolicy.wordMatchCount,
              'querySemantics': settings.matchPolicy.querySemantics.name,
              'anyOrder': settings.matchPolicy.anyOrder,
              'searchOptions': settings.searchOptions,
              'alternativeWords': settings.alternativeWords,
              'spacingValues': settings.spacingValues,
            },
            'witness': witness,
            'positiveWitnessSpans': overlaps,
          },
        ));
      }
    }
  }
  return PdfProximityProfileQueries(queries, {
    'recipe': pdfProximitySelectionRecipe,
    'candidatePairs': _candidatePairs,
    'attempts': attempts,
    'selectedPairs': witnesses,
    'selectionTimed': false,
    'builderTimed': false,
    'searchIndexExecuted': false,
    'nativeTextModified': false,
    'selectionUsesPerformance': false,
    'spanComputation': 'Proximity allMatches eagerly computes spans in both sources',
  });
}

InBookSearchSettings _settings(
  String query, {
  required int distance,
  required bool anyOrder,
}) => InBookSearchSettings(
  searchMode: SearchMode.advanced,
  distance: distance,
  matchPolicy: SearchMatchPolicy(anyOrder: anyOrder),
).forQuery(query);

String _hash(String value) => sha256.convert(utf8.encode(value)).toString();

// Shared by the preflight test and both profile variants. No timing selection.
// ignore_for_file: invalid_use_of_visible_for_testing_member
import 'package:otzaria/pdf_book/utils/pdf_user_regex_pattern.dart';
import 'package:otzaria/pdf_book/view/pdf_search_screen.dart';
import 'package:otzaria/search/utils/user_regex_query.dart';
import 'package:otzaria_search_engine/otzaria_search_engine.dart';
import 'package:pdfrx/pdfrx.dart';

const wrapSearchCandidates = [
  'בראשית',
  'אלהים',
  'ישראל',
  'משה',
  'ויאמר',
  'אמר',
  'הוא',
  'את',
];

typedef WrapSearchQuery = ({
  String id,
  RegExp pattern,
  Map<String, Object?> descriptor,
});

List<WrapSearchQuery> buildWrapSearchCandidate(String word) {
  if (!wrapSearchCandidates.contains(word)) {
    throw StateError('Query is outside the fixed candidate list: $word');
  }
  final literal = PdfBookSearchView.buildSimpleSearchPattern(
    word,
    wholeWord: false,
  );
  if (literal == null) {
    throw StateError('Application simple-pattern builder rejected $word');
  }
  // The literal builder emits its own regex dialect. A user types /word/,
  // not /literal.source/; pass that input through the real application parser.
  final userInput = '/$word/';
  final plan = UserRegexQuery.plan(userInput);
  final regex = PdfUserRegexPattern.tryParse(userInput);
  if (plan == null || plan.kind != UserRegexKind.singleWord || regex == null) {
    throw StateError('Application user-regex parser rejected $userInput');
  }
  return [
    (
      id: 'app-simple',
      pattern: literal.regExp,
      descriptor: {
        'query': word,
        'source': literal.source,
        'builder': 'PdfBookSearchView.buildSimpleSearchPattern',
        'runtimeType': '${literal.regExp.runtimeType}',
        'matchingSemantics': 'partial literal, wholeWord=false',
      },
    ),
    (
      id: 'app-user-regex',
      pattern: regex,
      descriptor: {
        'query': userInput,
        'source': regex.pattern,
        'builder': 'PdfUserRegexPattern.tryParse',
        'runtimeType': '${regex.runtimeType}',
        'planKind': plan.kind.name,
        'searchPattern': plan.pattern,
        'planWords': plan.words,
        'matchingSemantics': 'single-word user regex; own reference results',
      },
    ),
  ];
}

List<WrapSearchQuery> buildWrapSearchQueries(
  List<PdfPageText> texts,
  int target,
) {
  if (texts.length < 4 || target != texts.length * 3 ~/ 4) {
    throw StateError('Query selection needs the declared 75% wrap boundary');
  }
  final presence = <Map<String, Object?>>[];
  for (final word in wrapSearchCandidates) {
    final queries = buildWrapSearchCandidate(word);
    final literal = queries[0].pattern;
    final regex = queries[1].pattern;
    final simpleBefore = texts
        .take(target - 1)
        .any((text) => literal.hasMatch(text.fullText));
    final simpleAfter = texts
        .skip(target - 1)
        .any((text) => literal.hasMatch(text.fullText));
    final regexBefore = texts
        .take(target - 1)
        .any((text) => regex.hasMatch(text.fullText));
    final regexAfter = texts
        .skip(target - 1)
        .any((text) => regex.hasMatch(text.fullText));
    presence.add({
      'query': word,
      'simpleBefore': simpleBefore,
      'simpleAfter': simpleAfter,
      'regexBefore': regexBefore,
      'regexAfter': regexAfter,
    });
    if (simpleBefore && simpleAfter && regexBefore && regexAfter) {
      final selection = {
        'algorithm':
            'first fixed candidate with positive matches before and at/after 75%; no timings consulted',
        'candidateOrder': wrapSearchCandidates,
        'presenceChecks': presence,
        'wholeWord': false,
      };
      return [
        for (final query in queries)
          (
            id: query.id,
            pattern: query.pattern,
            descriptor: {...query.descriptor, 'selection': selection},
          ),
      ];
    }
  }
  throw StateError(
    'No fixed candidate is positive on both sides of the wrap boundary; no empty workload is permitted. $presence',
  );
}

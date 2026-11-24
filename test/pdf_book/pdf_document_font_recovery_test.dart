import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:otzaria/pdf_book/utils/pdf_document_font_recovery.dart';
import 'package:pdfrx/pdfrx.dart';

const _alreadyAvailable = PdfFontLoadResult(
  loaded: [],
  unresolved: [],
  failed: [],
  fontsReloaded: false,
);

PdfFontQuery _query(
  String face, {
  int weight = 400,
  bool italic = false,
  PdfFontCharset charset = PdfFontCharset.hebrew,
  int pitch = 0,
}) => PdfFontQuery(
  face: face,
  weight: weight,
  isItalic: italic,
  charset: charset,
  pitchFamily: pitch,
);

class _Document extends Fake implements PdfDocument {
  final controller = StreamController<PdfDocumentEvent>.broadcast(sync: true);

  @override
  Stream<PdfDocumentEvent> get events => controller.stream;

  void missing(List<PdfFontQuery> queries, {PdfDocument? owner}) {
    controller.add(PdfDocumentMissingFontsEvent(owner ?? this, queries));
  }

  @override
  Future<void> dispose() => controller.close();
}

class _Manager extends Fake implements PdfFontManager {
  final calls = <List<PdfFontQuery>>[];
  Future<PdfFontLoadResult> Function(List<PdfFontQuery>) load =
      (_) async => _alreadyAvailable;

  @override
  Future<PdfFontLoadResult> loadMissingFonts(
    Iterable<PdfFontQuery> queries, {
    PdfFontResolveContext context = const PdfFontResolveContext(),
    bool reloadFonts = true,
    PdfFontLoadProgressCallback? onProgress,
  }) {
    final snapshot = queries.toList();
    calls.add(snapshot);
    return load(snapshot);
  }
}

class _MutableQuery extends Fake implements PdfFontQuery {
  @override
  String face = 'original';
  @override
  int weight = 400;
  @override
  bool isItalic = false;
  @override
  PdfFontCharset charset = PdfFontCharset.hebrew;
  @override
  int pitchFamily = 0;
}

void main() {
  late _Document document;
  late _Manager manager;
  late PdfDocumentFontRecovery recovery;

  setUp(() {
    document = _Document();
    manager = _Manager();
    recovery = PdfDocumentFontRecovery(
      document,
      fontManager: manager,
      recoveredQueries: {},
    );
  });

  tearDown(() async {
    await recovery.dispose();
    await document.dispose();
  });

  test('דיווח מאוחר אחרי רישום עדיין מחייב refresh של המסמך', () async {
    await recovery.waitForPendingLoads();
    var refreshes = 0;
    recovery.onFontsAvailable = () async {
      refreshes++;
    };
    document.missing([_query('late')]);
    await recovery.waitForPendingLoads();

    expect(_alreadyAvailable.hasLoadedFonts, isFalse);
    expect(refreshes, 1);
    expect(recovery.availableQueries, {pdfFontQueryKey(_query('late'))});
    document.missing([_query('late')]);
    await recovery.waitForPendingLoads();
    expect(manager.calls, hasLength(1));
    expect(refreshes, 1);
  });

  test('דיווח ממסמך אחר אינו טוען גופן ואינו מרענן', () async {
    final other = _Document();
    addTearDown(other.dispose);
    var refreshes = 0;
    recovery.onFontsAvailable = () async {
      refreshes++;
    };
    document.missing([_query('foreign')], owner: other);
    await recovery.waitForPendingLoads();
    expect(manager.calls, isEmpty);
    expect(refreshes, 0);
  });

  test('משמר snapshot עצמאי של כל חמשת שדות השאילתה', () async {
    final query = _MutableQuery();
    final queries = <PdfFontQuery>[query];
    document.missing(queries);
    query
      ..face = 'changed'
      ..weight = 700
      ..isItalic = true
      ..charset = PdfFontCharset.ansi
      ..pitchFamily = 1;
    queries.clear();
    await recovery.waitForPendingLoads();

    expect(manager.calls, hasLength(1));
    expect(
      pdfFontQueryKey(manager.calls.single.single),
      ('original', 400, false, 177, 0),
    );
    expect(
      () => recovery.availableQueries.clear(),
      throwsUnsupportedError,
    );
  });

  test('סגנון ו-charset שונים לא מתאחדים לפי face', () async {
    final queries = [
      _query('face'),
      _query('face', weight: 700),
      _query('face', italic: true),
      _query('face', charset: PdfFontCharset.ansi),
      _query('face', pitch: 1),
    ];
    document.missing([...queries, _query('face')]);
    await recovery.waitForPendingLoads();
    expect(manager.calls.single, hasLength(5));
    expect(recovery.availableQueries, queries.map(pdfFontQueryKey).toSet());
  });

  test('שאילתה שכבר שימשה reopen אינה יוצרת לולאה', () async {
    await recovery.dispose();
    final query = _query('unsupported-glyph');
    final recovered = {pdfFontQueryKey(query)};
    recovery = PdfDocumentFontRecovery(
      document,
      fontManager: manager,
      recoveredQueries: recovered,
    );
    recovered.clear();
    document.missing([query]);
    await recovery.waitForPendingLoads();
    expect(manager.calls, isEmpty);
    expect(recovery.availableQueries, isEmpty);
  });

  test('כשל ושאילתה שלא נפתרה לא גורמים reopen וניתנים לניסיון חוזר', () async {
    final failed = _query('failed');
    final unresolved = _query('unresolved');
    manager.load = (_) async => PdfFontLoadResult(
      loaded: const [],
      unresolved: [unresolved],
      failed: [
        PdfFontLoadFailure(
          query: failed,
          error: StateError('font unavailable'),
          stackTrace: StackTrace.current,
        ),
      ],
      fontsReloaded: false,
    );
    var refreshes = 0;
    recovery.onFontsAvailable = () async {
      refreshes++;
    };
    document.missing([failed, unresolved]);
    await recovery.waitForPendingLoads();
    expect(recovery.availableQueries, isEmpty);
    expect(refreshes, 0);

    manager.load = (_) async => _alreadyAvailable;
    document.missing([failed, unresolved]);
    await recovery.waitForPendingLoads();
    expect(manager.calls, hasLength(2));
    expect(refreshes, 1);
  });

  test('סגירת המסמך מבטלת callback גם כשה-resolver טרם הסתיים', () async {
    final gate = Completer<PdfFontLoadResult>();
    manager.load = (_) => gate.future;
    var refreshes = 0;
    recovery.onFontsAvailable = () async {
      refreshes++;
    };
    document.missing([_query('slow')]);
    await Future<void>.delayed(Duration.zero);
    expect(manager.calls, hasLength(1));
    await document.dispose();
    await recovery.waitForPendingLoads().timeout(const Duration(seconds: 1));
    expect(document.controller.hasListener, isFalse);
    gate.complete(_alreadyAvailable);
    await Future<void>.delayed(Duration.zero);
    expect(refreshes, 0);
    expect(recovery.availableQueries, isEmpty);
  });

  test('dispose מנתק listener בלי לסגור מסמך בבעלות הצרכן', () async {
    await recovery.dispose();
    expect(document.controller.hasListener, isFalse);
    expect(document.controller.isClosed, isFalse);
    document.missing([_query('after-dispose')]);
    await Future<void>.delayed(Duration.zero);
    expect(manager.calls, isEmpty);
  });

  test('מסמך אחר אינו ממתין לטעינת גופן אטית של הראשון', () async {
    final gate = Completer<PdfFontLoadResult>();
    manager.load = (queries) => queries.single.face == 'slow'
        ? gate.future
        : Future.value(_alreadyAvailable);
    final other = _Document();
    final otherRecovery = PdfDocumentFontRecovery(
      other,
      fontManager: manager,
      recoveredQueries: {},
    );
    addTearDown(() async {
      await otherRecovery.dispose();
      await other.dispose();
    });
    var otherRefreshes = 0;
    otherRecovery.onFontsAvailable = () async {
      otherRefreshes++;
    };
    document.missing([_query('slow')]);
    await Future<void>.delayed(Duration.zero);
    other.missing([_query('fast')]);
    await otherRecovery.waitForPendingLoads();
    expect(otherRefreshes, 1);
    expect(gate.isCompleted, isFalse);
    gate.complete(_alreadyAvailable);
    await recovery.waitForPendingLoads();
  });
}

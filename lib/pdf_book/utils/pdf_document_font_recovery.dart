import 'dart:async';

import 'package:otzaria/core/error_log_file.dart';
import 'package:pdfrx/pdfrx.dart';

/// זהות השאילתה כוללת סגנון ו-charset, ולא רק את שם הגופן.
typedef PdfFontQueryKey = (String, int, bool, int, int);

PdfFontQueryKey pdfFontQueryKey(PdfFontQuery query) => (
  query.face,
  query.weight,
  query.isItalic,
  query.charset.pdfiumCharsetId,
  query.pitchFamily,
);

/// מטפל רק בדיווחים של [document], עד שהמסמך או ההאזנה נסגרים.
///
/// אין כאן בעלות על המסמך: הצרכן מחליט מתי בטוח לפתוח אותו מחדש. בפרט,
/// צרכן שמחלץ טקסט ממתין לסיום הקריאה לפני החלפת המסמך.
class PdfDocumentFontRecovery {
  PdfDocumentFontRecovery(
    this.document, {
    required PdfFontManager fontManager,
    required Set<PdfFontQueryKey> recoveredQueries,
  }) : _manager = fontManager,
       _recoveredQueries = Set.unmodifiable(recoveredQueries) {
    _subscription = document.events.listen(_onEvent, onDone: _onDone);
  }

  final PdfDocument document;
  final PdfFontManager _manager;
  final Set<PdfFontQueryKey> _recoveredQueries;
  final _queued = <PdfFontQueryKey, PdfFontQuery>{};
  final _loading = <PdfFontQueryKey>{};
  final _available = <PdfFontQueryKey>{};
  final _closedSignal = Completer<void>();
  late final StreamSubscription<PdfDocumentEvent> _subscription;
  Future<void>? _pending;
  Future<void> Function()? _onFontsAvailable;
  bool _notified = false;
  bool _closed = false;

  Set<PdfFontQueryKey> get availableQueries => Set.unmodifiable(_available);

  /// נקרא פעם אחת למסמך; הבעלים חייב לבדוק שזה עדיין המסמך הפעיל שלו.
  set onFontsAvailable(Future<void> Function() callback) {
    if (_closed) return;
    _onFontsAvailable = callback;
    if (_available.isNotEmpty) _schedule();
  }

  void _onEvent(PdfDocumentEvent event) {
    if (_closed ||
        event is! PdfDocumentMissingFontsEvent ||
        !identical(event.document, document)) {
      return;
    }
    for (final query in event.missingFonts) {
      final key = pdfFontQueryKey(query);
      if (_recoveredQueries.contains(key) ||
          _available.contains(key) ||
          _loading.contains(key)) {
        continue;
      }
      // לא שומרים את רשימת האירוע או אובייקט שאילתה שמקורו בקורא אחר.
      _queued[key] = PdfFontQuery(
        face: key.$1,
        weight: key.$2,
        isItalic: key.$3,
        charset: PdfFontCharset.fromPdfiumCharsetId(key.$4),
        pitchFamily: key.$5,
      );
    }
    if (_queued.isNotEmpty) _schedule();
  }

  void _schedule() {
    if (_closed || _pending != null) return;
    _pending = Future<void>.microtask(_load).whenComplete(() {
      _pending = null;
      if (!_closed && _queued.isNotEmpty) _schedule();
    });
  }

  Future<void> _load() async {
    while (!_closed && _queued.isNotEmpty) {
      final batch = Map<PdfFontQueryKey, PdfFontQuery>.of(_queued);
      _queued.clear();
      _loading.addAll(batch.keys);
      try {
        final result = await _manager.loadMissingFonts(batch.values);
        if (_closed) return;
        final unavailable = <PdfFontQueryKey>{
          for (final query in result.unresolved) pdfFontQueryKey(query),
          for (final failure in result.failed) pdfFontQueryKey(failure.query),
        };
        // אירוע מאוחר עשוי להגיע אחרי שמסמך אחר כבר רשם את הגופן. במקרה
        // הזה loaded ריק, אך השאילתה הצליחה והמסמך הישן עדיין דורש reopen.
        _available.addAll(batch.keys.where((key) => !unavailable.contains(key)));
      } catch (error, stackTrace) {
        // כשל prepare/reload אינו מבטל האזנה לאירוע הבא או ניסיון חוזר.
        ErrorLogFile.append(
          title: 'PDF font fallback: loading document fonts failed',
          error: error,
          stackTrace: stackTrace,
        );
      } finally {
        _loading.removeAll(batch.keys);
      }
    }
    final callback = _onFontsAvailable;
    if (!_closed && !_notified && _available.isNotEmpty && callback != null) {
      _notified = true;
      await callback();
    }
  }

  /// מוסר replay/דיווח שנוצר בקריאת native שזה עתה הסתיימה, וממתין רק
  /// לטעינות שהמסמך הזה ביקש. אין המתנה לפתיחות או לגילוי במסמכים אחרים.
  Future<void> waitForPendingLoads() async {
    await Future<void>.delayed(Duration.zero);
    while (!_closed && _pending != null) {
      await Future.any([_pending!, _closedSignal.future]);
    }
  }

  void _onDone() {
    if (_closed) return;
    _closed = true;
    _closedSignal.complete();
    _queued.clear();
    _onFontsAvailable = null;
  }

  Future<void> dispose() {
    _onDone();
    return _subscription.cancel();
  }
}

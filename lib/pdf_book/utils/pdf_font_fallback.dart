import 'dart:async';

import 'package:flutter/services.dart' show rootBundle;
import 'package:otzaria/theme/app_fonts.dart';
import 'package:pdfrx/pdfrx.dart';

import 'pdf_document_font_recovery.dart';

/// פתיחת PDF כשלגופנים שאינם מוטמעים בקובץ נרשם גופן אמיתי.
///
/// pdfrx אינו פונה לגופני המערכת בעצמו: גופן לא מוטמע שלא נרשם מוחלף בגופן
/// פנימי בלי עברית, והאותיות נעלמות גם מהתצוגה וגם מהטקסט (חיפוש, אינדוקס).
class PdfFontFallback {
  PdfFontFallback._();

  static const String bundledFallbackAsset = 'fonts/Tinos-Regular.ttf';

  static final PdfFontManager _manager = PdfFontManager.platform(
    resolvers: [
      const _SystemFamilyResolver(),
      const _BundledFontResolver(),
    ],
  );

  /// פותח את [filePath] אחרי טיפול בדיווחי הגופנים של הפתיחה הזו בלבד.
  /// צרכן של פעולות מאוחרות משתמש ב-[openReader] או ב-[documentRef].
  static Future<PdfDocument> openFile(
    String filePath, {
    PdfPasswordProvider? passwordProvider,
    bool useProgressiveLoading = false,
  }) async {
    final opened = (await _openWithFonts(
      _PdfFileOpener(
        filePath,
        passwordProvider: passwordProvider,
        useProgressiveLoading: useProgressiveLoading,
      ),
      <PdfFontQueryKey>{},
    ))!;
    await opened.recovery.dispose();
    return opened.document;
  }

  /// בעלות על מסמך לחילוץ: דיווח מאוחר יכול לחייב קריאה מחדש של העמוד.
  static Future<PdfFontFallbackReader> openReader(
    String filePath, {
    PdfPasswordProvider? passwordProvider,
    bool useProgressiveLoading = false,
  }) async {
    final opener = _PdfFileOpener(
      filePath,
      passwordProvider: passwordProvider,
      useProgressiveLoading: useProgressiveLoading,
    );
    final recovered = <PdfFontQueryKey>{};
    return PdfFontFallbackReader._(
      opener,
      (await _openWithFonts(opener, recovered))!,
      recovered,
    );
  }

  /// הפניה למסמך עבור [PdfViewer], עם אותו מפתח שמשמש [PdfDocumentRefFile].
  static PdfDocumentRef documentRef(
    String filePath, {
    PdfPasswordProvider? passwordProvider,
    bool useProgressiveLoading = true,
  }) {
    return _PdfFontDocumentRef(
      _PdfFileOpener(
        filePath,
        passwordProvider: passwordProvider,
        useProgressiveLoading: useProgressiveLoading,
      ),
    );
  }

  static Future<_OpenedPdf?> _openWithFonts(
    _PdfFileOpener opener,
    Set<PdfFontQueryKey> recovered, {
    Completer<void>? cancellation,
  }) async {
    while (true) {
      if (cancellation?.isCompleted ?? false) return null;
      final document = await opener.open();
      if (cancellation?.isCompleted ?? false) {
        await document.dispose();
        return null;
      }
      final recovery = PdfDocumentFontRecovery(
        document,
        fontManager: _manager,
        recoveredQueries: recovered,
      );
      var delivered = false;
      try {
        if (cancellation == null) {
          await recovery.waitForPendingLoads();
        } else {
          await Future.any([
            recovery.waitForPendingLoads(),
            cancellation.future,
          ]);
        }
        if (cancellation?.isCompleted ?? false) return null;
        if (recovery.availableQueries.isEmpty) {
          delivered = true;
          return (document: document, recovery: recovery);
        }
        recovered.addAll(recovery.availableQueries);
      } finally {
        if (!delivered) {
          await recovery.dispose();
          // open/native של המסמך הסתיימו; resolver אטי אינו מחזיק אותו.
          await document.dispose();
        }
      }
    }
  }

  /// שם המשפחה ששמור בשם גופן של PDF: בלי קידומת subset ובלי סיומת סגנון.
  static String familyFromPdfFace(String face) {
    final plus = face.indexOf('+');
    final base = plus >= 0 ? face.substring(plus + 1) : face;
    final styleStart = base.indexOf(RegExp('[-,]'));
    return (styleStart > 0 ? base.substring(0, styleStart) : base).trim();
  }
}

typedef _OpenedPdf = ({
  PdfDocument document,
  PdfDocumentFontRecovery recovery,
});

class _PdfFileOpener {
  _PdfFileOpener(
    this.path, {
    required this.passwordProvider,
    required this.useProgressiveLoading,
  });

  final String path;
  final PdfPasswordProvider? passwordProvider;
  final bool useProgressiveLoading;
  String? _acceptedPassword;

  Future<PdfDocument> open() async {
    final accepted = _acceptedPassword;
    var triedAccepted = false;
    String? suppliedPassword;
    final document = await PdfDocument.openFile(
      path,
      passwordProvider: passwordProvider == null && accepted == null
          ? null
          : () async {
              if (!triedAccepted && accepted != null) {
                triedAccepted = true;
                return suppliedPassword = accepted;
              }
              return suppliedPassword = await passwordProvider?.call();
            },
      useProgressiveLoading: useProgressiveLoading,
    );
    // לא שומרים סיסמה שגויה. רק open שהצליח מאשר את הסיסמה האחרונה.
    if (document.isEncrypted && suppliedPassword != null) {
      _acceptedPassword = suppliedPassword;
    }
    return document;
  }
}

/// אותו מקור ואותו listenable משותף משמשים גם את הקורא וגם את ה-preview.
class _PdfFontDocumentRef extends PdfDocumentRef {
  _PdfFontDocumentRef(this._opener)
    : super(key: PdfDocumentRefKey(_opener.path));

  final _PdfFileOpener _opener;
  int _generation = 0;
  Set<PdfFontQueryKey>? _nextRecovered;

  @override
  PdfPasswordProvider? get passwordProvider => _opener.passwordProvider;

  @override
  bool get firstAttemptByEmptyPassword => true;

  @override
  Future<PdfDocument> loadDocument(
    PdfDocumentLoaderProgressCallback progressCallback,
  ) async {
    final generation = ++_generation;
    final listenable = resolveListenable();
    final recovered = _nextRecovered ?? <PdfFontQueryKey>{};
    _nextRecovered = null;
    await pdfrxFlutterInitialize();
    // המסמך נמסר לפני טעינת הגופן. כך סגירת המאזין האחרון יכולה לסגור
    // אותו גם בזמן resolver אטי, ו-load אינו מחזיק פתיחה לא נראית בחיים.
    final document = await _opener.open();
    final recovery = PdfDocumentFontRecovery(
      document,
      fontManager: PdfFontFallback._manager,
      recoveredQueries: recovered,
    );
    recovery.onFontsAvailable = () async {
      // מאפשרים ל-load למסור את המסמך גם אם replay הסתיים באותו microtask.
      await Future<void>.delayed(Duration.zero);
      if (generation != _generation ||
          !identical(listenable.document, document)) {
        return;
      }
      _nextRecovered = {
        ...recovered,
        ...recovery.availableQueries,
      };
      // נשמר listenable זה, לא controller שעלול כבר להציג ספר אחר.
      await listenable.load(forceReload: true);
    };
    return document;
  }
}

/// צרכן סדרתי של PDF: המסמך האמיתי נשאר חי עד שהקריאה הפעילה מסתיימת.
///
/// [read] עשויה לחזור על הפעולה לאחר רישום גופן של אותו מסמך. מחזירים ממנה
/// טקסט/נתונים עצמאיים, ולא שומרים PdfPage או handle לשימוש מחוץ לפעולה.
class PdfFontFallbackReader {
  PdfFontFallbackReader._(this._opener, this._opened, this._recovered);

  final _PdfFileOpener _opener;
  _OpenedPdf _opened;
  final Set<PdfFontQueryKey> _recovered;
  final _cancellation = Completer<void>();
  Future<void> _tail = Future<void>.value();
  Future<void>? _disposeFuture;
  bool _closed = false;

  PdfDocument get document => _opened.document;

  Future<T> read<T>(Future<T> Function(PdfDocument document) action) {
    if (_closed) return Future.error(StateError('PDF reader is closed.'));
    final completer = Completer<T>();
    _tail = _tail.then((_) async {
      try {
        if (_closed) throw StateError('PDF reader is closed.');
        while (true) {
          final result = await action(_opened.document);
          await _opened.recovery.waitForPendingLoads();
          final available = _opened.recovery.availableQueries;
          if (_closed || available.isEmpty) {
            completer.complete(result);
            return;
          }
          final recovered = {..._recovered, ...available};
          final replacement = await PdfFontFallback._openWithFonts(
            _opener,
            recovered,
            cancellation: _cancellation,
          );
          if (replacement == null) {
            completer.complete(result);
            return;
          }
          if (_closed) {
            await replacement.recovery.dispose();
            await replacement.document.dispose();
            completer.complete(result);
            return;
          }
          final old = _opened;
          _opened = replacement;
          _recovered
            ..clear()
            ..addAll(recovered);
          await old.recovery.dispose();
          await old.document.dispose();
        }
      } catch (error, stackTrace) {
        completer.completeError(error, stackTrace);
      }
    });
    return completer.future;
  }

  Future<void> dispose() {
    _closed = true;
    if (!_cancellation.isCompleted) _cancellation.complete();
    return _disposeFuture ??= () async {
      await _opened.recovery.dispose();
      // יכול להיקרא אחרי timeout כש-read עוד ב-native. אין סגירה תחתיו.
      await _tail;
      await _opened.document.dispose();
    }();
  }
}

/// גופן מערכת שמשפחתו תואמת לשם שבקובץ (David, Narkisim וכו').
class _SystemFamilyResolver implements PdfFontResolver {
  const _SystemFamilyResolver();

  @override
  Future<PdfFontResolution?> resolve(
    PdfFontQuery query,
    PdfFontResolveContext context,
  ) async {
    await AppFonts.warmUpSystemFontsCache();
    final faces = AppFonts.pluginSystemFamilyFacesByCompactName(
      PdfFontFallback.familyFromPdfFace(query.face),
    );
    if (faces == null) return null;
    final isBold =
        query.weight >= 600 || query.face.toLowerCase().contains('bold');
    return PdfFontResolution.localFontFile(
      fontFilePath: isBold
          ? faces.boldPath ?? faces.regularPath
          : faces.regularPath,
      targetFace: query.face,
      resolvedFace: faces.family,
    );
  }
}

/// מוצא אחרון, גם באנדרואיד שאין בו גופני Windows: כל גופן אמיתי עם עברית
/// מחזיר את האותיות, ו-Tinos מכסה גם לטינית.
class _BundledFontResolver implements PdfFontResolver {
  const _BundledFontResolver();

  @override
  PdfFontResolution resolve(
    PdfFontQuery query,
    PdfFontResolveContext context,
  ) {
    return PdfFontResolution(
      loadData: ({onProgress}) async {
        final data = await rootBundle.load(
          PdfFontFallback.bundledFallbackAsset,
        );
        return data.buffer.asUint8List(data.offsetInBytes, data.lengthInBytes);
      },
      targetFace: query.face,
      resolvedFace: 'Tinos',
    );
  }
}

import 'dart:async';
import 'dart:ui';

import 'package:flutter_test/flutter_test.dart';
import 'package:otzaria/pdf_book/utils/pdf_book_layout.dart';
import 'package:pdfrx/pdfrx.dart';

class _FakePage extends Fake implements PdfPage {
  _FakePage(this.width, this.height);

  @override
  double width;

  @override
  double height;
}

class _CountingPage extends Fake implements PdfPage {
  _CountingPage(this._width, this._height);

  final double _width;
  final double _height;
  int sizeReads = 0;

  @override
  double get width {
    sizeReads++;
    return _width;
  }

  @override
  double get height {
    sizeReads++;
    return _height;
  }
}

class _SnapshotDocument extends Fake implements PdfDocument {
  _SnapshotDocument(this._pages);

  List<PdfPage> _pages;
  final _events = StreamController<PdfDocumentEvent>.broadcast();

  @override
  List<PdfPage> get pages => _pages;

  @override
  Stream<PdfDocumentEvent> get events => _events.stream;

  void publish(List<PdfPage> pages) {
    _pages = List.unmodifiable(pages);
    _events.add(PdfDocumentPageStatusChangedEvent(this, changes: const {}));
  }

  @override
  Future<void> dispose() async {
    await _events.close();
  }
}

void main() {
  for (final count in [100, 1000, 10000]) {
    test('מסמך PDFium עם $count עמודים אינו קורא מידות בפגיעות מטמון', () {
      final pages = List<PdfPage>.unmodifiable([
        for (var i = 0; i < count; i++)
          _CountingPage(400.0 + i % 3, 600.0 + i % 5),
      ]);
      final document = _SnapshotDocument(pages);
      addTearDown(document.dispose);
      final memo = BookViewLayoutMemo();
      final first = memo.layoutNativeDocumentPages(
        pages,
        document: document,
        hasCover: true,
        verticalMargin: 16,
      );
      expect(
        pages.fold(0, (sum, page) => sum + (page as _CountingPage).sizeReads),
        count * 2,
      );
      for (final page in pages.cast<_CountingPage>()) {
        page.sizeReads = 0;
      }
      for (var i = 0; i < 200; i++) {
        expect(
          memo.layoutNativeDocumentPages(
            pages,
            document: document,
            hasCover: true,
            verticalMargin: 16,
          ),
          same(first),
        );
      }
      expect(
        pages.fold(0, (sum, page) => sum + (page as _CountingPage).sizeReads),
        0,
      );
      expect(() => first.pageLayouts[0] = Rect.zero, throwsUnsupportedError);
    });
  }

  test('רשימה שפורסמה נצפית עוד לפני מסירת אירוע העדכון', () async {
    final document = _SnapshotDocument(
      List.unmodifiable([_FakePage(400, 600)]),
    );
    addTearDown(document.dispose);
    var delivered = false;
    final subscription = document.events.listen((_) => delivered = true);
    addTearDown(subscription.cancel);
    final memo = BookViewLayoutMemo();
    final first = memo.layoutNativeDocumentPages(
      document.pages,
      document: document,
      hasCover: true,
      verticalMargin: 16,
    );

    document.publish([_FakePage(700, 900)]);
    expect(delivered, isFalse);
    final changed = memo.layoutNativeDocumentPages(
      document.pages,
      document: document,
      hasCover: true,
      verticalMargin: 16,
    );
    expect(changed, isNot(same(first)));
    expect(changed.pageLayouts.single.size, const Size(700, 900));
    await Future<void>.delayed(Duration.zero);
    expect(delivered, isTrue);
    expect(
      memo.layoutNativeDocumentPages(
        document.pages,
        document: document,
        hasCover: true,
        verticalMargin: 16,
      ),
      same(changed),
    );
  });

  test('בעלות מסמך חדש נבדקת גם אם הוצגה אותה רשימת עמודים', () async {
    final page = _CountingPage(400, 600);
    final pages = List<PdfPage>.unmodifiable([page]);
    final first = _SnapshotDocument(pages);
    final second = _SnapshotDocument(pages);
    addTearDown(first.dispose);
    addTearDown(second.dispose);
    final memo = BookViewLayoutMemo();
    memo.layoutNativeDocumentPages(
      pages,
      document: first,
      hasCover: true,
      verticalMargin: 16,
    );
    page.sizeReads = 0;
    memo.layoutNativeDocumentPages(
      pages,
      document: second,
      hasCover: true,
      verticalMargin: 16,
    );
    expect(page.sizeReads, 2);
    first.publish([_FakePage(900, 1000)]);
    await Future<void>.delayed(Duration.zero);
    page.sizeReads = 0;
    final retained = memo.layoutNativeDocumentPages(
      pages,
      document: second,
      hasCover: true,
      verticalMargin: 16,
    );
    expect(page.sizeReads, 0);
    expect(retained.pageLayouts.single.size, const Size(400, 600));
  });

  test('מקור לא מאומת או רשימה מועתקת ממשיכים לבדוק מידות', () {
    final page = _FakePage(400, 600);
    final source = List<PdfPage>.unmodifiable([page]);
    final document = _SnapshotDocument(source);
    addTearDown(document.dispose);
    final memo = BookViewLayoutMemo();
    final copied = List<PdfPage>.of(source);
    for (final owner in [document, null]) {
      final first = memo.layoutNativeDocumentPages(
        copied,
        document: owner,
        hasCover: true,
        verticalMargin: 16,
      );
      page.height += 100;
      final changed = memo.layoutNativeDocumentPages(
        copied,
        document: owner,
        hasCover: true,
        verticalMargin: 16,
      );
      expect(changed, isNot(same(first)));
      expect(changed.pageLayouts.single.height, page.height);
    }
  });

  test('פרסום גאומטריה, סדר, מספר עמודים ופרמטרים משמר את הפריסה', () {
    final document = _SnapshotDocument(const []);
    addTearDown(document.dispose);
    final memo = BookViewLayoutMemo();
    final a = _FakePage(400, 600);
    final b = _FakePage(300, 700);
    final c = _FakePage(600, 400);
    for (final pages in <List<PdfPage>>[
      [a, b],
      [c, b],
      [b, c],
      [b, c, a],
      [b],
      [],
    ]) {
      document.publish(pages);
      for (final (hasCover, margin) in [
        (true, 16.0),
        (false, 16.0),
        (false, 8.0),
      ]) {
        final actual = memo.layoutNativeDocumentPages(
          document.pages,
          document: document,
          hasCover: hasCover,
          verticalMargin: margin,
        );
        final expected = buildBookViewPageLayout(
          pageSizes: [for (final page in pages) Size(page.width, page.height)],
          hasCover: hasCover,
          verticalMargin: margin,
        );
        expect(actual, expected);
      }
    }
  });

  test('ניקוי המטמון מחייב בדיקת מקור מחדש', () {
    final page = _CountingPage(400, 600);
    final document = _SnapshotDocument(List.unmodifiable([page]));
    addTearDown(document.dispose);
    final memo = BookViewLayoutMemo();
    final first = memo.layoutNativeDocumentPages(
      document.pages,
      document: document,
      hasCover: true,
      verticalMargin: 16,
    );
    memo.clear();
    page.sizeReads = 0;
    final second = memo.layoutNativeDocumentPages(
      document.pages,
      document: document,
      hasCover: true,
      verticalMargin: 16,
    );
    expect(second, isNot(same(first)));
    expect(page.sizeReads, 2);
  });

  test('בנייה חדשה ושינוי פרמטרים קוראים את מידות כל עמוד פעם אחת', () {
    const count = 10000;
    final pages = [
      for (var i = 0; i < count; i++)
        _CountingPage(400.0 + i % 3, 600.0 + i % 5),
    ];
    final memo = BookViewLayoutMemo();
    for (final (hasCover, margin) in [
      (true, 16.0),
      (false, 16.0),
      (false, 8.0),
    ]) {
      for (final page in pages) {
        page.sizeReads = 0;
      }
      final layout = memo.layout(
        pages,
        hasCover: hasCover,
        verticalMargin: margin,
      );
      expect(pages.fold(0, (sum, page) => sum + page.sizeReads), count * 2);
      expect(layout.pageLayouts, hasLength(count));
      for (var i = 0; i < count; i++) {
        expect(layout.pageLayouts[i].width, 400.0 + i % 3);
        expect(layout.pageLayouts[i].height, 600.0 + i % 5);
      }
    }
  });

  test('פריסה שמורה עדיין נבדקת בסריקת מידות אחת ומחזירה אותו מופע', () {
    final pages = [for (var i = 0; i < 10000; i++) _CountingPage(400, 600)];
    final memo = BookViewLayoutMemo();
    final first = memo.layout(pages, hasCover: true, verticalMargin: 16);
    for (final page in pages) {
      page.sizeReads = 0;
    }

    final cached = memo.layout(pages, hasCover: true, verticalMargin: 16);

    expect(cached, same(first));
    expect(
      pages.fold(0, (sum, page) => sum + page.sizeReads),
      pages.length * 2,
    );
  });

  test('מידות זהות — אותו מופע פריסה', () {
    final memo = BookViewLayoutMemo();
    final pages = [for (var i = 0; i < 5; i++) _FakePage(400, 600)];
    final first = memo.layout(pages, hasCover: true, verticalMargin: 16);
    expect(
      memo.layout(List.of(pages), hasCover: true, verticalMargin: 16),
      same(first),
    );
  });

  test('עמוד שנמדד מחדש (אותו אובייקט, מידה אחרת) — פריסה חדשה', () {
    final memo = BookViewLayoutMemo();
    final pages = [for (var i = 0; i < 5; i++) _FakePage(400, 600)];
    final first = memo.layout(pages, hasCover: true, verticalMargin: 16);
    pages[3].height = 700;
    final second = memo.layout(pages, hasCover: true, verticalMargin: 16);
    expect(second, isNot(same(first)));
    expect(second.pageLayouts[3].height, 700);
  });

  test('שינוי כריכה, מרווח או מספר עמודים — פריסה חדשה', () {
    final memo = BookViewLayoutMemo();
    final pages = [for (var i = 0; i < 5; i++) _FakePage(400, 600)];
    final first = memo.layout(pages, hasCover: true, verticalMargin: 16);
    expect(
      memo.layout(pages, hasCover: false, verticalMargin: 16),
      isNot(same(first)),
    );
    final second = memo.layout(pages, hasCover: false, verticalMargin: 8);
    expect(second.pageLayouts, hasLength(5));
    expect(
      memo.layout(pages.sublist(1), hasCover: false, verticalMargin: 8),
      isNot(same(second)),
    );
  });
}
